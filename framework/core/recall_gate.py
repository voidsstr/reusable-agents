"""Recall gate: CPSC recalls and product-safety warnings -> per-site recall matches.

WHY THIS EXISTS
---------------
An affiliate site must never show a buy button for a product the U.S.
Consumer Product Safety Commission says to stop using. Recalls hit top-selling
brands (a 25,030-unit power-station recall in 2025, a CPSC warning about
station fires on units sold on Amazon), and a site that keeps promoting a
recalled SKU is both a safety problem and an Associates-account liability.
This module is the deterministic half of that gate. It pulls the public CPSC
data, decides which catalog rows a recall or warning covers, assigns a tier
and writes the result into each subscribed site's database, where the site's
renderer hides the buy button (see ``SCHEMA_SQL`` for the contract views).

It NEVER calls an LLM. Matching is brand + model regex/token work that can be
explained in one sentence per match; anything it cannot decide goes to a
manual queue for a human. The module imports no LLM client, and
``agents/recall-gate-agent`` refuses every AgentBase LLM helper.

SOURCES (all public, read-only GETs)
------------------------------------
``saferproducts_api``  https://www.saferproducts.gov/RestWebServices/Recall
                       JSON, filtered by ``LastPublishDateStart/End``. The
                       freshest recall source (it carries a recall the day it
                       is announced) and the only one with the CPSC page URL
                       and image captions. Products[].Model is almost always
                       empty, so model numbers come from the text.
``cpsc_recall_csv``    https://www.cpsc.gov/s3fs-public/recall-data/recalls_recall_listing.csv
                       Every recall since 1973 (~10k rows, ~18 MB), refreshed
                       about weekly. Carries the normalised "Remedy Type"
                       and the link to an earlier product-safety warning.
``cpsc_warning_csv``   https://www.cpsc.gov/s3fs-public/recall-data/product_safety_warning_listing.csv
                       CPSC product-safety warnings: products whose maker
                       refused or ignored a recall. Not in the API at all.
The CSVs are fetched with If-Modified-Since against a local cache. The HTML
pages under cpsc.gov/Recalls and cpsc.gov/Warnings sit behind a bot wall and
are never needed.

Record ids are namespaced and stable: ``cpsc-recall-<digits>`` (the API's
"26010" and the CSV's "26-010" are one record) and ``cpsc-warning-<digits>``.

MATCHING (per site, against the rows its ``catalog_sql`` returns)
-----------------------------------------------------------------
Text is normalised (accents, case, "2000-Watt" -> "2000w", possessives) and
tokenised; list separators (',', ';', '/') and sentence ends are kept beside
the tokens. Only the fields that describe the affected product are searched:
title, product names/models, description, image captions. Remedy, hazard,
contact and sold-at text is never searched for models, so "contact Aiper to
receive a free replacement Scuba S1" does not match the Scuba S1. A bare
catalog number matches the recall's number+unit and the other way round
("DELTA Max 2000" ~ "DELTA Max 2000W", "500W" ~ "500").

``exact``      the recall names the catalog row's ASIN or UPC; or its model
               number (or a kit component's number) while naming the row's
               brand (the component's own brand, for a kit) as its subject;
               or the brand is the subject AND every distinguishing token of
               the model name appears inside one short window with no variant
               marker beside it ("Delta Max" is not "Delta Max 2000"). List
               naming is not a variant: "Explorer 1000, 1500 and 2000" names
               all three, "Q20 and Q20 Pro" both, and so does an ELIDED list
               whose shared leading words stand once: "Explorer 1000 and 1000
               Plus", "Prowler Pro / Pro Crew", "Explorer 1000 Pro and 2000
               Pro" (see ``Matcher._windows`` for the exact shape). A shorter
               catalog name is a variant only where EVERY mention of it is
               part of a mention of a longer one ("HALO 1000" inside "HALO
               1000 Bolt").
``sibling``    same brand, same family/line, different variant; or, for a
               row named by a number alone ("300W Portable Power Station"),
               a recall of the same brand naming other numbers ("400W and
               500W"): the renamed-model safety net.
``ambiguous``  model evidence only in a compatibility context ("battery packs
               for S1 Pro") or where the recall scopes it out ("The Explorer
               1000 Plus is not included in this recall"); a number found only
               as a rating in running text; a catalog row with no model
               detail; a brand-wide recall that names no model (specs in the
               description and generic words like "LiFePO4" do not name one);
               a recall naming models none of which maps to the site's models
               of that brand (``unmapped_models``); a strong model number of
               a row whose brand the recall does not name (a sub-brand, a
               legal name); or model-NAME evidence on a recall outside the
               site's categories (identifier evidence ignores categories).
A brand mentioned only as a compatibility reference ("replacement batteries
for Dyson vacuums") is ignored by default. Pure-digit model numbers never
match a unit count ("About 25,030 units").

TIERS
-----
Recall-level tiers come from ordered config rules (first match wins). The
defaults: a CPSC warning is ``stop-use``; a remedy made only of repair /
inspect / new-instructions / label (firmware fixes included) is
``repair-hold`` for 90 days from the recall date (``hold_anchor``
"first_match"/"later" count from when the product was first matched, which
is the run's ``now`` for a new match); anything else is ``stop-use``. An
exact match takes the recall's tier with status ``active``. Sibling and
ambiguous matches get tier ``review`` and status ``manual-review`` (the
manual queue) with the would-be tier kept in ``proposed_tier``.

One-directional review precedence (``plan_match_update``,
``plan_stale_update``): the gate never LIFTS or clears a match by itself, and
it always APPLIES a stricter block, over a pending request or a review
decision too. When its new result would demote an ``active`` row, or it no
longer finds the match for a catalog row it still lists, it keeps the
decision and sets ``review_requested_at`` (with ``gate_proposal``), which puts
the row in the manual queue and emails the operator. When it blocks harder
than the stored decision (a cleared row whose evidence is now stronger AND
blocking; a recall re-announced with a stricter remedy, which reaches every
active row of it, confirmed ones included), it applies the block, keeps the
human decision in ``gate_proposal`` (``gate-escalated-over-review``), asks for
a review and emails. Otherwise a review decision is never overwritten.

Buy button: blocked while ``status='active'`` and the tier is ``stop-use``, or
``repair-hold`` before ``hold_until``. Picks exclude every match that is not
``cleared``. Both rules ship as views in ``SCHEMA_SQL`` and the views are the
only rule a site may render from.

CONFIG (lowest to highest precedence)
-------------------------------------
    DEFAULTS (this module)
      <- config/recall-gate-config.json (repo default, replaced WHOLESALE by
         the framework-storage copy at the same key)
Per site: ``by_site.<site>`` names a ``profile`` (niche values: categories,
generic words, variant words, brand aliases) and carries the site's
``catalog_sql``, which must return EVERY product whose page the site still
renders, retired or inactive ones included (do not filter on is_active): a
product it leaves out is treated as having no page. Dicts deep-merge, lists
replace, keys starting with ``_`` are comments. The site's DSN env var name
comes from ``dsn_env`` when set, else from ``framework.core.site_registry``.
DSN values are never logged. Email recipients come from the operator env vars
the fleet already sets (see ``email_addresses``), never from code.

CLI
---
    python3 -m framework.core.recall_gate schema [--site SITE]
    python3 -m framework.core.recall_gate config [--site SITE] [--no-storage]
    python3 -m framework.core.recall_gate fetch [--source NAME] [--out FILE]
    python3 -m framework.core.recall_gate match --site SITE --recalls FILE --catalog FILE
    python3 -m framework.core.recall_gate queue --site SITE [--json]
    python3 -m framework.core.recall_gate review --site SITE --match-key KEY
            --decision confirm|clear|reopen --reviewer NAME [--note TEXT] [--tier T]
``fetch`` and ``match`` touch no database; ``queue`` reads one; ``review``
records a human decision in one. ``queue``/``review`` find the site's DSN env
var name the way the agent does (by_site.<site>.dsn_env, else the site
registry WITH its storage override; ``--no-storage`` skips storage). Exit
codes: 0 ok, 1 not found, 2 invalid (or the site is locked by a sync).
"""
from __future__ import annotations

import argparse
import copy
import csv
import datetime as _dt
import gzip
import hashlib
import html
import io
import json
import logging
import os
import re
import string
import sys
import tempfile
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, fields as dc_fields, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

log = logging.getLogger("framework.recall_gate")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "1"
# 3: elided list naming ("Explorer 1000 and 1000 Plus"); supersede by covered words.
MATCHER_VERSION = "3"
CONFIG_KEY = "config/recall-gate-config.json"
_REPO_ROOT = Path(__file__).resolve().parents[2]
REPO_CONFIG_PATH = _REPO_ROOT / "config" / "recall-gate-config.json"

KIND_RECALL = "recall"
KIND_WARNING = "warning"
KINDS = (KIND_RECALL, KIND_WARNING)

TIER_STOP_USE = "stop-use"
TIER_REPAIR_HOLD = "repair-hold"
TIER_REVIEW = "review"
TIERS = (TIER_STOP_USE, TIER_REPAIR_HOLD, TIER_REVIEW)
BLOCKING_TIERS = (TIER_STOP_USE, TIER_REPAIR_HOLD)

MATCH_EXACT = "exact"
MATCH_SIBLING = "sibling"
MATCH_AMBIGUOUS = "ambiguous"
MATCH_KINDS = (MATCH_EXACT, MATCH_SIBLING, MATCH_AMBIGUOUS)
_KIND_RANK = {MATCH_AMBIGUOUS: 1, MATCH_SIBLING: 2, MATCH_EXACT: 3}

STATUS_ACTIVE = "active"
STATUS_REVIEW = "manual-review"
STATUS_CLEARED = "cleared"
STATUSES = (STATUS_ACTIVE, STATUS_REVIEW, STATUS_CLEARED)

REMEDY_OPTIONS = ("refund", "replace", "repair", "dispose", "inspect",
                  "new-instructions", "label", "no-remedy")

SOURCE_API = "saferproducts_api"
SOURCE_RECALL_CSV = "cpsc_recall_csv"
SOURCE_WARNING_CSV = "cpsc_warning_csv"
SOURCES = (SOURCE_API, SOURCE_RECALL_CSV, SOURCE_WARNING_CSV)

DEFAULT_TABLES = {"recalls": "recalls", "matches": "recall_matches",
                  "sync_log": "recall_sync_log"}

# Expected CSV headers. A header that drifts away from these fails the source
# loudly instead of silently producing empty records.
RECALL_CSV_REQUIRED = ("Recall Number", "Date", "Recall Heading", "Name of product",
                       "Description", "Remedy Type", "Remedy")
WARNING_CSV_REQUIRED = ("Product Safety Warning Number", "Product Safety Warning Date",
                        "Product Safety Warning Title", "Name of product", "Description")

DEFAULTS: dict[str, Any] = {
    "schema_version": SCHEMA_VERSION,
    "enabled": True,
    "sources": {
        SOURCE_API: {
            "enabled": True,
            "url": "https://www.saferproducts.gov/RestWebServices/Recall",
            # Every run re-reads this many days of LastPublishDate, so a recall
            # stays in the fresh set until the weekly CSV carries it.
            "window_days": 21,
            # First run (no prior success): how far back to read.
            "initial_lookback_days": 730,
            # Extra SaferProducts query filters (e.g. {"ProductType": "..."}).
            # Empty by design: one date-window pull serves every site and each
            # site's profile filters locally, so no niche wording is missed.
            "params": {},
            "timeout_s": 90,
        },
        SOURCE_RECALL_CSV: {
            "enabled": True,
            "url": "https://www.cpsc.gov/s3fs-public/recall-data/recalls_recall_listing.csv",
            "timeout_s": 180,
            # A truncated download must not pass for the whole history.
            "min_rows": 1000,
        },
        SOURCE_WARNING_CSV: {
            "enabled": True,
            "url": "https://www.cpsc.gov/s3fs-public/recall-data/product_safety_warning_listing.csv",
            "timeout_s": 90,
            "min_rows": 10,
        },
    },
    "http": {
        "user_agent": "Mozilla/5.0 (compatible; RecallGateBot/1.0)",
        "retries": 2,
        "retry_base_delay_s": 2.0,
        # Local copy of each CSV for If-Modified-Since. Missing or unwritable:
        # every run downloads the file in full.
        "cache_dir": "~/.cache/reusable-agents/recall-gate",
    },
    "matching": {
        # All distinguishing tokens of a model name must fall in a window of
        # at most this many tokens.
        "max_span": 8,
        # How many tokens before a mention are searched for a compatibility cue.
        "compat_lookback": 4,
        "compat_cues": ["for", "compatible with", "works with", "for use with", "fits",
                        "fit", "replacement", "replacements", "replaces", "used in",
                        "used with", "use with", "designed for", "intended for", "not"],
        # A brand named only after a compatibility cue: "ignore" or "review".
        "compat_reference_matches": "ignore",
        # Exact model-NAME evidence on a recall outside the site's categories:
        # "review" (ambiguous, manual queue), "ignore", or "exact". Identifier
        # evidence (ASIN, UPC, model or kit-component number) ignores this.
        "out_of_category": "review",
        # A mention the recall itself scopes out ("The Explorer 1000 Plus is
        # not included in this recall") counts as a reference, not as the
        # recalled product. Applied to the tokens after a mention, up to the
        # end of its sentence; 'which/that/...' clauses about something else
        # do not count.
        "exclusion_after_regex": (
            r"^(?:(?!(?:which|that|who|whose|where|when|while|but|although|if)\b)[a-z0-9]+ ){0,5}"
            r"(?:is|are|was|were)(?: also)? not (?:included|involved|affected|subject|part|covered"
            r"|impacted|recalled|being recalled|under)\b"),
        "exclusion_cues": ["excluding", "not including", "with the exception of"],
        # The brand is the recall's subject and the recall names models, but
        # none of them matches any of this site's models of that brand:
        # "review" queues the brand's in-category rows (the recalled model may
        # be one of ours under another name), "ignore" does not.
        "unmapped_models": "review",
        # Pure-digit model numbers shorter than max(5, this) need a "model"
        # cue right before them; mixed letter+digit ones need 3 characters.
        "min_model_number_len": 4,
        "model_number_cues": ["model", "models", "item", "no", "number", "sku",
                              "style", "part"],
        # Words that never distinguish one model from another (merged with the
        # profile's generic_words).
        "generic_words": ["the", "a", "an", "and", "or", "with", "for", "of", "in", "on",
                          "by", "to", "new", "model", "models", "series", "edition",
                          "version", "color", "black", "white", "gray", "grey", "silver",
                          "blue", "red", "green", "pack", "set", "bundle", "combo", "inch",
                          "inches"],
        # A window that sits next to one of these (or next to a short number)
        # names a different variant: "Seagull" vs "Seagull Pro".
        "variant_words": ["pro", "max", "plus", "mini", "ultra", "lite", "light", "se",
                          "xl", "elite", "air", "neo", "go", "prime", "smart", "turbo",
                          "duo", "dual", "omni", "ii", "iii", "iv"],
    },
    "remedy_options": {
        # CPSC option codes -> normalised option. Unknown codes are kept raw
        # and ignored; then the remedy text decides (text_patterns).
        "aliases": {
            "refund": "refund", "refunds": "refund",
            "replace": "replace", "replacement": "replace",
            "repair": "repair", "repairs": "repair",
            "dispose": "dispose", "disposal": "dispose",
            "inspect": "inspect", "inspection": "inspect",
            "new instructions": "new-instructions",
            "label": "label", "labels": "label", "labeling": "label",
            "no remedy available": "no-remedy", "no remedy": "no-remedy",
        },
        "text_patterns": {
            "refund": r"\brefunds?\b",
            "replace": r"\breplacements?\b|\breplaced?\b",
            "repair": r"\brepairs?\b|\bfirmware\b|\bsoftware update\b|\bretrofit\b",
            "dispose": r"\bdispos(e|al|ing)\b|\bdiscard\b|\bdestroy\b",
            "inspect": r"\binspect(ion)?\b",
            "new-instructions": r"\bnew instructions\b|\bupdated (instructions|manual)\b",
            "label": r"\bnew (warning )?labels?\b",
            "no-remedy": r"\bno remedy\b|\bnot agreed to\b|\bunresponsive\b",
        },
    },
    "tiers": {
        "rules": [
            {"id": "warning", "when": {"kind": [KIND_WARNING]}, "tier": TIER_STOP_USE},
            {"id": "repair-remedy",
             "when": {"remedy_options_subset_of": ["repair", "inspect",
                                                   "new-instructions", "label"],
                      "remedy_options_nonempty": True},
             "tier": TIER_REPAIR_HOLD, "hold_days": 90, "hold_anchor": "published"},
            {"id": "default", "when": {}, "tier": TIER_STOP_USE},
        ],
    },
    "email": {
        "enabled": True,
        # The fleet-wide operator pair (CLAUDE.md "Outbound-email recipient
        # policy") is read from the env vars the fleet host already sets for
        # operator alerts, in the SAME order framework.core.resilience
        # (notify_operator) and dispatch use: first set name wins. Literal
        # "to"/"from" values override them.
        "to": [],
        "from": "",
        "to_env": ["FRAMEWORK_OPERATOR_EMAIL", "OPERATOR_EMAIL"],
        "from_env": ["IMPLEMENTER_FROM", "OPERATOR_FROM_EMAIL"],
        "msmtp_account": "automation",
        # A match on a recalled product is an alert, not digest traffic.
        "bypass_digest": True,
        # Individual emails per run; the rest go out as one summary email.
        "max_per_run": 25,
        "subject_prefix": "[recall-gate]",
        "alert_on_live_buy_button": True,
    },
    "goals": {"sync_age_max_h": 26, "manual_queue_max_age_h": 72},
    "site_defaults": {
        "enabled": False,
        "profile": "",
        "dsn_env": "",
        "dsn_include_aliases": False,
        "tables": dict(DEFAULT_TABLES),
        "catalog_sql": "",
        "catalog_params": {},
        "statement_timeout_ms": 60000,
        "connect_timeout_s": 15,
        "post_sync_sql": [],
        "email": {"enabled": True},
        "buy_button": {
            # Read-only SQL (run in a READ ONLY transaction) returning the
            # blocked SKUs the site's own data still renders a buy button for.
            "sql": "",
            "http": {
                "enabled": False,
                # {asin} {product_id} {model_id} {slug}; empty = the catalog
                # row's url column.
                "url_template": "",
                "markers": [],
                "headers": {},
                "timeout_s": 20,
                # Blocked SKUs beyond this many count as unverified.
                "max_probes": 50,
                # Hosts the probe may GET, besides the site registry's domain
                # (and www.<domain>). Anything else is never fetched.
                "allowed_hosts": [],
                # Same-host redirects followed; a redirect off the site is
                # never followed (it is checked against the markers instead).
                "max_redirects": 3,
            },
        },
    },
    "profiles": {},
    "by_site": {},
}

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RecallGateError(Exception):
    """Base error for the recall gate."""


class RecallGateConfigError(RecallGateError):
    """The configuration cannot be used."""


class SourceError(RecallGateError):
    """A CPSC source could not be fetched or parsed."""


class SiteLockedError(RecallGateError):
    """Another recall-gate run holds the site's advisory lock."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")


def _ident(name: Any, what: str) -> str:
    if not isinstance(name, str) or not _IDENT_RE.match(name):
        raise RecallGateConfigError(f"unsafe {what} identifier {name!r}")
    return name


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _iso(ts: Optional[_dt.datetime]) -> Optional[str]:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=_dt.timezone.utc)
    return ts.astimezone(_dt.timezone.utc).isoformat(timespec="seconds")


def _parse_ts(value: Any) -> Optional[_dt.datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, _dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=_dt.timezone.utc)
    if isinstance(value, _dt.date):
        return _dt.datetime.combine(value, _dt.time(0), tzinfo=_dt.timezone.utc)
    s = str(value).strip().replace("Z", "+00:00")
    try:
        ts = _dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=_dt.timezone.utc)


def _as_date(value: Any) -> Optional[_dt.date]:
    if value is None or value == "":
        return None
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, _dt.date):
        return value
    try:
        return _dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _merge(base: Mapping[str, Any], over: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    """Deep-merge ``over`` into a copy of ``base``: dicts merge, everything
    else (lists included) replaces; keys starting with '_' are comments."""
    out = copy.deepcopy(dict(base))
    for k, v in (over or {}).items():
        if isinstance(k, str) and k.startswith("_"):
            continue
        if isinstance(v, Mapping) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _stable_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      default=str)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _dedupe(values: Iterable[str]) -> tuple[str, ...]:
    """Order-preserving dedupe on the normalised form; empty values dropped."""
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        v = clean_text(v)
        if not v:
            continue
        k = norm_text(v)
        if k in seen:
            continue
        seen.add(k)
        out.append(v)
    return tuple(out)


def _as_list(value: Any) -> list[str]:
    """A catalog cell that may be NULL, a list, a JSON array or a delimited
    string -> list of non-empty strings."""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set, frozenset)):
        return [str(v).strip() for v in value if v is not None and str(v).strip()]
    s = str(value).strip()
    if not s:
        return []
    if s.startswith("[") and s.endswith("]"):
        try:
            doc = json.loads(s)
            if isinstance(doc, list):
                return [str(v).strip() for v in doc if v is not None and str(v).strip()]
        except ValueError:
            pass
    if s.startswith("{") and s.endswith("}"):  # Postgres array literal as text
        s = s[1:-1]
    return [p.strip().strip('"') for p in re.split(r"[;,|]", s) if p.strip().strip('"')]


def email_addresses(email_cfg: Mapping[str, Any],
                    environ: Optional[Mapping[str, str]] = None) -> tuple[list[str], str]:
    """(recipients, sender) for operator email: literal config values when
    set, else the first non-empty env var named in ``to_env`` / ``from_env``.
    Either may come back empty (email then waits for the env to be set)."""
    env = os.environ if environ is None else environ
    to = [str(a).strip() for a in (email_cfg.get("to") or []) if str(a).strip()]
    if not to:
        for name in email_cfg.get("to_env") or []:
            value = str(env.get(str(name)) or "").strip()
            if value:
                to = [a.strip() for a in value.split(",") if a.strip()]
                break
    sender = str(email_cfg.get("from") or "").strip()
    if not sender:
        for name in email_cfg.get("from_env") or []:
            value = str(env.get(str(name)) or "").strip()
            if value:
                sender = value
                break
    return to, sender


# ---------------------------------------------------------------------------
# Text normalisation
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]*>")
_ATTR_JUNK_RE = re.compile(r"\b[a-z][a-z-]*=\"[^\"]*\"\s*>", re.I)
_WS_RE = re.compile(r"\s+")


def clean_text(value: Any) -> str:
    """Display text: HTML entities decoded (twice: the CSV double-encodes
    some), tags and truncated-tag debris dropped, whitespace collapsed."""
    if value is None:
        return ""
    s = str(value)
    if not s:
        return ""
    s = html.unescape(html.unescape(s))
    s = s.replace(" ", " ").replace(" ", " ").replace("﻿", "")
    s = _TAG_RE.sub(" ", s)
    s = _ATTR_JUNK_RE.sub(" ", s)
    return _WS_RE.sub(" ", s).strip()


_POSSESSIVE_RE = re.compile(r"(\w)['’‘`]s\b")
_THOUSANDS_RE = re.compile(r"(\d),(\d{3})\b")
_UNIT_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(\d)\s*-?\s*(?:watt[\s-]?hours?|wh)\b"), r"\1wh"),
    (re.compile(r"(\d)\s*-?\s*(?:kilowatt[\s-]?hours?|kwh)\b"), r"\1kwh"),
    (re.compile(r"(\d)\s*-?\s*(?:watts?|w)\b"), r"\1w"),
    (re.compile(r"(\d)\s*-?\s*(?:amp[\s-]?hours?|ah)\b"), r"\1ah"),
    (re.compile(r"(\d)\s*-?\s*(?:mah)\b"), r"\1mah"),
    (re.compile(r"(\d)\s*-?\s*(?:volts?|v)\b"), r"\1v"),
)
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def norm_text(value: Any) -> str:
    """Matching form: accents stripped, lower case, '&' -> 'and', possessive
    's dropped, thousands separators dropped, number+unit joined
    ("2000-Watt" -> "2000w", "500 W" -> "500w")."""
    s = clean_text(value)
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    s = s.replace("&", " and ")
    s = _POSSESSIVE_RE.sub(r"\1", s)
    s = _THOUSANDS_RE.sub(r"\1\2", s)
    for rx, rep in _UNIT_RULES:
        s = rx.sub(rep, s)
    return s


def tokenize(value: Any) -> tuple[str, ...]:
    return tuple(_TOKEN_RE.findall(norm_text(value)))


# Between two tokens: a list separator ("1000, 1500", "1000/1500", "A; B")
# and a sentence or clause end ("... recall. The ...", "...: ").
_LIST_GAP_RE = re.compile(r"[,;/]")
_STOP_GAP_RE = re.compile(r"[.!?;:]\s")  # "2.5" and "recall.The" are not stops
# Words that join list items ("Q20 and Q20 Pro", "1000 or 1500").
_LIST_WORDS = frozenset({"and", "or"})


def tokenize_with_gaps(value: Any) -> tuple[tuple[str, ...], frozenset[int], frozenset[int]]:
    """(tokens, seps, stops) of ``norm_text(value)``: ``seps`` holds i when a
    list separator (',', ';', '/') stands between token i and token i+1,
    ``stops`` holds i when a sentence or clause ends after token i. The
    tokens equal ``tokenize(value)``."""
    s = norm_text(value)
    toks: list[str] = []
    seps: set[int] = set()
    stops: set[int] = set()
    prev_end: Optional[int] = None
    for m in _TOKEN_RE.finditer(s):
        if prev_end is not None:
            gap = s[prev_end:m.start()]
            if gap.strip():
                i = len(toks) - 1
                if _LIST_GAP_RE.search(gap):
                    seps.add(i)
                if _STOP_GAP_RE.search(gap):
                    stops.add(i)
        toks.append(m.group(0))
        prev_end = m.end()
    return tuple(toks), frozenset(seps), frozenset(stops)


def compact(value: Any) -> str:
    """Model-number form: alphanumerics only ("EFD-310" -> "efd310")."""
    return "".join(_TOKEN_RE.findall(norm_text(value)))


# norm_text joins a number and its unit ("2000-Watt" -> "2000w"); the stem is
# the number alone. A catalog "DELTA Max 2000" must find a recall's
# "DELTA Max 2000W", and a catalog "500W" a recall's bare "500".
_UNIT_STEM_RE = re.compile(r"^(\d+)(?:w|wh|kwh|v|ah|mah)$")


def _unit_stem(tok: str) -> Optional[str]:
    m = _UNIT_STEM_RE.match(tok)
    return m.group(1) if m else None


def _numeric_like(tok: str) -> bool:
    """A bare number or a number with a unit ("2000", "400w", "1000wh")."""
    return tok.isdigit() or _unit_stem(tok) is not None


def _has_digit(tok: str) -> bool:
    return any(c.isdigit() for c in tok)


def _has_alpha(tok: str) -> bool:
    return any(c.isalpha() for c in tok)


_MEASURE_TOKEN_RE = re.compile(
    r"^(\d+(mah|v|ah|lbs?|oz|in|ft|mm|cm|m|kg|g|ml|l|pcs|pc|pk|ct|st|nd|rd|th|s|am|pm"
    r"|hz|mph|kph|f|c|gal|qt|yr|yrs|mo|days?|hrs?|min)|\d{1,2}x)$")


def _is_identifier(tok: str) -> bool:
    """A token that names a specific model or variant: letters AND digits,
    and not a plain measurement ("24lbs", "3pm", "2x"). Watt and watt-hour
    tokens ("400w", "1000wh") do identify stations, and "3000x" is a model."""
    return (len(tok) >= 2 and _has_digit(tok) and _has_alpha(tok)
            and not _MEASURE_TOKEN_RE.match(tok))


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

_TUPLE_FIELDS = ("product_names", "product_models", "image_captions", "images",
                 "manufacturers", "importers", "distributors", "other_companies",
                 "manufactured_in", "upcs", "remedy_options_raw", "remedy_options",
                 "related_ids", "sources")


@dataclass(frozen=True)
class Recall:
    """One CPSC recall or product-safety warning, normalised across sources.

    Text fields hold display text (``clean_text``). ``published_at`` and
    ``last_published_at`` are ISO dates."""

    id: str
    kind: str
    number: str
    title: str
    published_at: Optional[str] = None
    last_published_at: Optional[str] = None
    url: str = ""
    url_derived: bool = False
    description: str = ""
    announcement: str = ""
    product_names: tuple[str, ...] = ()
    product_models: tuple[str, ...] = ()
    image_captions: tuple[str, ...] = ()
    images: tuple[str, ...] = ()
    manufacturers: tuple[str, ...] = ()
    importers: tuple[str, ...] = ()
    distributors: tuple[str, ...] = ()
    other_companies: tuple[str, ...] = ()
    hazard: str = ""
    remedy: str = ""
    consumer_action: str = ""
    remedy_options_raw: tuple[str, ...] = ()
    remedy_options: tuple[str, ...] = ()
    units: str = ""
    units_count: Optional[int] = None
    incidents: str = ""
    sold_at: str = ""
    manufactured_in: tuple[str, ...] = ()
    upcs: tuple[str, ...] = ()
    related_ids: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()

    @property
    def number_key(self) -> str:
        return self.id.rsplit("-", 1)[-1]

    @property
    def source_label(self) -> str:
        return "cpsc-warning" if self.kind == KIND_WARNING else "cpsc-recall"

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in dc_fields(self):
            v = getattr(self, f.name)
            out[f.name] = list(v) if isinstance(v, tuple) else v
        return out

    @classmethod
    def from_dict(cls, doc: Mapping[str, Any]) -> "Recall":
        known = {f.name for f in dc_fields(cls)}
        kw: dict[str, Any] = {}
        for k, v in (doc or {}).items():
            if k not in known:
                continue
            if k in _TUPLE_FIELDS:
                kw[k] = tuple(str(x) for x in (v or []) if x is not None)
            elif k == "units_count":
                kw[k] = int(v) if isinstance(v, (int, float)) or (
                    isinstance(v, str) and v.isdigit()) else None
            elif k == "url_derived":
                kw[k] = bool(v)
            elif k in ("published_at", "last_published_at"):
                kw[k] = str(v)[:10] if v else None
            else:
                kw[k] = "" if v is None else str(v)
        for req in ("id", "kind", "number", "title"):
            kw.setdefault(req, "")
        return cls(**kw)

    def content_sha(self) -> str:
        """Hash of the recall's content; ``sources`` excluded so the same
        content seen through another source is not a change."""
        doc = self.to_dict()
        doc.pop("sources", None)
        return _sha256(_stable_json(doc))

    def companies(self) -> tuple[str, ...]:
        return self.manufacturers + self.importers + self.distributors + self.other_companies


def recall_id(kind: str, number: Any, *, fallback: str = "") -> str:
    digits = re.sub(r"\D", "", str(number or ""))
    prefix = "cpsc-warning" if kind == KIND_WARNING else "cpsc-recall"
    if digits:
        return f"{prefix}-{digits}"
    return f"{prefix}-x{hashlib.sha1(fallback.encode('utf-8')).hexdigest()[:10]}"


def _display_number(number: Any) -> str:
    s = clean_text(number)
    if re.fullmatch(r"\d{5}", s):
        return f"{s[:2]}-{s[2:]}"
    return s


_DATE_FORMATS = ("%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%m/%d/%Y", "%Y-%m-%d")


def parse_date(value: Any) -> Optional[str]:
    """CPSC dates ("October 09, 2025", "2025-10-09T00:00:00") -> ISO date."""
    s = clean_text(value)
    if not s:
        return None
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            return None
    for fmt in _DATE_FORMATS:
        try:
            return _dt.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


_UNITS_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(million|thousand)?", re.I)


def parse_units(value: Any) -> Optional[int]:
    """"About 32,660 (In addition, ...)" -> 32660; "About 2.5 million" ->
    2500000; no number -> None."""
    m = _UNITS_RE.search(clean_text(value))
    if not m:
        return None
    try:
        num = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    mult = {"million": 1_000_000, "thousand": 1_000}.get((m.group(2) or "").lower(), 1)
    return int(round(num * mult))


def _fiscal_year(iso_date: Optional[str]) -> Optional[int]:
    d = _as_date(iso_date)
    if d is None:
        return None
    return d.year + 1 if d.month >= 10 else d.year


def derive_cpsc_url(kind: str, title: str, published_at: Optional[str]) -> str:
    """Best-effort cpsc.gov page URL for a record that came without one (the
    CSVs carry none). CPSC files pages under the federal fiscal year with a
    slug of the title; measured against 941 API records this reproduces the
    page in ~92% of cases. Callers flag it ``url_derived``."""
    fy = _fiscal_year(published_at)
    t = clean_text(title)
    if not fy or not t:
        return ""
    t = unicodedata.normalize("NFKD", t)
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    t = re.sub(r"['’‘`]", "", t)
    t = re.sub(r"[^A-Za-z0-9\s-]", " ", t)
    slug = re.sub(r"-{2,}", "-", re.sub(r"\s+", "-", t.strip())).strip("-")
    section = "Warnings" if kind == KIND_WARNING else "Recalls"
    return f"https://www.cpsc.gov/{section}/{fy}/{slug}"


# -- SaferProducts API -------------------------------------------------------

def _names(items: Any, key: str = "Name") -> list[str]:
    out: list[str] = []
    for it in items or []:
        if isinstance(it, Mapping):
            v = it.get(key)
            if v:
                out.append(str(v))
        elif isinstance(it, str) and it:
            out.append(it)
    return out


def _upc_values(items: Any) -> list[str]:
    out: list[str] = []
    for it in items or []:
        if isinstance(it, Mapping):
            for k, v in it.items():
                if "upc" in str(k).lower() and v:
                    out.append(str(v))
        elif it:
            out.append(str(it))
    return out


def parse_api_records(payload: Any) -> list[Recall]:
    """SaferProducts ``/RestWebServices/Recall?format=json`` -> records.
    Raises SourceError on anything but a JSON array of recall objects."""
    if isinstance(payload, (bytes, bytearray)):
        try:
            payload = json.loads(bytes(payload).decode("utf-8-sig"))
        except ValueError as e:
            raise SourceError(f"SaferProducts API: response is not JSON ({e})") from None
    elif isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError as e:
            raise SourceError(f"SaferProducts API: response is not JSON ({e})") from None
    if not isinstance(payload, list):
        msg = payload.get("Message") if isinstance(payload, Mapping) else type(payload).__name__
        raise SourceError(f"SaferProducts API: expected a JSON array, got {str(msg)[:200]}")
    out: list[Recall] = []
    for raw in payload:
        if not isinstance(raw, Mapping):
            continue
        title = clean_text(raw.get("Title"))
        number = raw.get("RecallNumber") or raw.get("RecallID")
        published = parse_date(raw.get("RecallDate"))
        products = [p for p in (raw.get("Products") or []) if isinstance(p, Mapping)]
        units = next((clean_text(p.get("NumberOfUnits")) for p in products
                      if clean_text(p.get("NumberOfUnits"))), "")
        url = clean_text(raw.get("URL"))
        rid = recall_id(KIND_RECALL, number, fallback=f"{title}|{published}")
        out.append(Recall(
            id=rid, kind=KIND_RECALL, number=_display_number(number), title=title,
            published_at=published,
            last_published_at=parse_date(raw.get("LastPublishDate")),
            url=url or derive_cpsc_url(KIND_RECALL, title, published),
            url_derived=not url,
            description=clean_text(" ".join(
                [str(raw.get("Description") or "")]
                + [str(p.get("Description") or "") for p in products])),
            product_names=_dedupe(_names(products)),
            product_models=_dedupe(_names(products, "Model")),
            image_captions=_dedupe(_names(raw.get("Images"), "Caption")),
            images=tuple(u for u in (_names(raw.get("Images"), "URL")) if u),
            manufacturers=_dedupe(_names(raw.get("Manufacturers"))),
            importers=_dedupe(_names(raw.get("Importers"))),
            distributors=_dedupe(_names(raw.get("Distributors"))),
            hazard=clean_text(" ".join(_names(raw.get("Hazards")))),
            remedy=clean_text(" ".join(_names(raw.get("Remedies")))),
            remedy_options_raw=_dedupe(_names(raw.get("RemedyOptions"), "Option")),
            units=units, units_count=parse_units(units),
            incidents=clean_text(" ".join(_names(raw.get("Injuries")))),
            sold_at=clean_text(" ".join(_names(raw.get("Retailers")))),
            manufactured_in=_dedupe(_names(raw.get("ManufacturerCountries"), "Country")),
            upcs=tuple(sorted({re.sub(r"\D", "", u) for u in _upc_values(raw.get("ProductUPCs"))
                               if re.sub(r"\D", "", u)})),
            sources=(SOURCE_API,),
        ))
    return out


# -- CPSC CSV exports --------------------------------------------------------

def _csv_reader(data: Any, required: Sequence[str], label: str) -> csv.DictReader:
    if isinstance(data, (bytes, bytearray)):
        text = bytes(data).decode("utf-8-sig", errors="replace")
    else:
        text = str(data or "")
        if text.startswith("﻿"):
            text = text[1:]
    csv.field_size_limit(max(csv.field_size_limit(), 16 * 1024 * 1024))
    reader = csv.DictReader(io.StringIO(text, newline=""))
    header = [h.strip() for h in (reader.fieldnames or [])]
    missing = [c for c in required if c not in header]
    if missing:
        raise SourceError(f"{label}: unexpected CSV header, missing {missing}")
    reader.fieldnames = header
    return reader


def _company_list(value: Any) -> tuple[str, ...]:
    v = clean_text(value)
    return (v,) if v else ()


def _custom_company(row: Mapping[str, Any]) -> tuple[str, ...]:
    label = clean_text(row.get("Custom Label")).lower()
    value = clean_text(row.get("Custom Field"))
    if value and any(k in label for k in ("seller", "retailer", "importer",
                                          "manufacturer", "distributor", "brand")):
        return (value,)
    return ()


def parse_recall_csv(data: Any) -> list[Recall]:
    """The CPSC recall listing CSV -> records (one per row)."""
    out: list[Recall] = []
    for row in _csv_reader(data, RECALL_CSV_REQUIRED, "CPSC recall CSV"):
        title = clean_text(row.get("Recall Heading"))
        number = clean_text(row.get("Recall Number"))
        if not title and not number:
            continue
        published = parse_date(row.get("Date"))
        warning_no = clean_text(row.get("Product Safety Warning Number"))
        units = clean_text(row.get("Units"))
        out.append(Recall(
            id=recall_id(KIND_RECALL, number, fallback=f"{title}|{published}"),
            kind=KIND_RECALL, number=number, title=title, published_at=published,
            url=derive_cpsc_url(KIND_RECALL, title, published), url_derived=True,
            description=clean_text(row.get("Description")),
            announcement=clean_text(row.get("Original Product Safety Warning Announcement")),
            product_names=_dedupe([row.get("Name of product") or ""]),
            manufacturers=_company_list(row.get("Manufacturers")),
            importers=_company_list(row.get("Importers")),
            distributors=_company_list(row.get("Distributors")),
            other_companies=_custom_company(row),
            hazard=clean_text(row.get("Hazard Description")),
            remedy=clean_text(row.get("Remedy")),
            consumer_action=clean_text(row.get("Consumer Action")),
            remedy_options_raw=_dedupe(p for p in re.split(r"[;,]", clean_text(
                row.get("Remedy Type"))) if p.strip()),
            units=units, units_count=parse_units(units),
            incidents=clean_text(row.get("Incidents")),
            sold_at=clean_text(row.get("Sold At")),
            manufactured_in=_dedupe([row.get("Manufactured In") or ""]),
            related_ids=((recall_id(KIND_WARNING, warning_no),) if re.sub(r"\D", "", warning_no)
                         else ()),
            sources=(SOURCE_RECALL_CSV,),
        ))
    return out


def parse_warning_csv(data: Any) -> list[Recall]:
    """The CPSC product-safety-warning listing CSV -> records (kind warning)."""
    out: list[Recall] = []
    for row in _csv_reader(data, WARNING_CSV_REQUIRED, "CPSC warning CSV"):
        title = clean_text(row.get("Product Safety Warning Title"))
        number = clean_text(row.get("Product Safety Warning Number"))
        if not title and not number:
            continue
        published = parse_date(row.get("Product Safety Warning Date"))
        units = clean_text(row.get("Units"))
        action = clean_text(row.get("Consumer Action"))
        out.append(Recall(
            id=recall_id(KIND_WARNING, number, fallback=f"{title}|{published}"),
            kind=KIND_WARNING, number=number, title=title, published_at=published,
            url=derive_cpsc_url(KIND_WARNING, title, published), url_derived=True,
            description=clean_text(row.get("Description")),
            announcement=clean_text(row.get("Original Product Safety Warning Announcement")),
            product_names=_dedupe([row.get("Name of product") or ""]),
            manufacturers=_company_list(row.get("Manufacturers")),
            importers=_company_list(row.get("Importers")),
            distributors=_company_list(row.get("Distributors")),
            other_companies=_custom_company(row),
            hazard=clean_text(row.get("Hazard Description")),
            remedy=action, consumer_action=action,
            units=units, units_count=parse_units(units),
            incidents=clean_text(row.get("Incidents")),
            sold_at=clean_text(row.get("Sold At")),
            manufactured_in=_dedupe([row.get("Manufactured In") or ""]),
            sources=(SOURCE_WARNING_CSV,),
        ))
    return out


# -- merging -----------------------------------------------------------------

_UNION_FIELDS = ("sources", "related_ids", "upcs", "images", "remedy_options_raw")


def merge_recall(base: Recall, newer: Recall) -> Recall:
    """Field-wise merge of two views of one record. ``newer`` wins for a
    non-empty value that differs from ``base`` beyond formatting; list fields
    are unioned; a real URL beats a derived one. Re-merging what a site
    already stores with the same source data does not churn it."""
    if base.id != newer.id:
        raise ValueError(f"cannot merge {base.id} with {newer.id}")
    kw: dict[str, Any] = {}
    for f in dc_fields(Recall):
        name = f.name
        a, b = getattr(base, name), getattr(newer, name)
        if name in ("id", "kind", "url", "url_derived"):
            continue
        if name == "number":
            kw[name] = a if "-" in a or not b else b
        elif name in _TUPLE_FIELDS:
            if name in _UNION_FIELDS:
                kw[name] = tuple(dict.fromkeys(list(a) + list(b)))
            else:
                kw[name] = _dedupe(list(a) + list(b))
        elif name == "last_published_at":
            kw[name] = max([x for x in (a, b) if x], default=None)
        elif name == "units_count":
            kw[name] = b if b is not None else a
        elif b and (not a or norm_text(a) != norm_text(b)):
            kw[name] = b
        else:
            kw[name] = a
    kw["id"], kw["kind"] = base.id, base.kind
    if base.url and not base.url_derived:
        kw["url"], kw["url_derived"] = base.url, False
    elif newer.url and not newer.url_derived:
        kw["url"], kw["url_derived"] = newer.url, False
    else:
        kw["url"] = newer.url or base.url
        kw["url_derived"] = bool(kw["url"])
    return Recall(**kw)


def merge_records(*groups: Iterable[Recall]) -> dict[str, Recall]:
    """Merge record lists in precedence order (later groups win field-wise)
    and link warnings to the recalls that superseded them."""
    out: dict[str, Recall] = {}
    for group in groups:
        for rec in group or ():
            prev = out.get(rec.id)
            out[rec.id] = rec if prev is None else merge_recall(prev, rec)
    back: dict[str, list[str]] = {}
    for rec in out.values():
        for rel in rec.related_ids:
            back.setdefault(rel, []).append(rec.id)
    for rid, linked in back.items():
        if rid in out:
            rec = out[rid]
            out[rid] = replace(rec, related_ids=tuple(dict.fromkeys(
                list(rec.related_ids) + sorted(linked))))
    return out


# ---------------------------------------------------------------------------
# Remedies and tiers
# ---------------------------------------------------------------------------

def normalize_remedy_options(rec: Recall, remedy_cfg: Mapping[str, Any]) -> tuple[str, ...]:
    """Normalised remedy options: known CPSC codes first; when none is
    recognised (the API sometimes sends "R" or a sentence), the remedy text
    decides through ``text_patterns``."""
    aliases = {str(k).lower(): str(v) for k, v in (remedy_cfg.get("aliases") or {}).items()}
    found: set[str] = set()
    for raw in rec.remedy_options_raw:
        key = clean_text(raw).lower().strip(" .")
        if key in aliases:
            found.add(aliases[key])
    if not found:
        text = norm_text(" ".join(x for x in (rec.remedy, rec.consumer_action) if x)
                         + " " + " ".join(r for r in rec.remedy_options_raw if len(r) > 25))
        for opt, pattern in (remedy_cfg.get("text_patterns") or {}).items():
            try:
                if re.search(pattern, text):
                    found.add(str(opt))
            except re.error:
                log.warning("recall_gate: bad remedy text pattern for %s", opt)
    return tuple(o for o in REMEDY_OPTIONS if o in found) + tuple(
        sorted(o for o in found if o not in REMEDY_OPTIONS))


def with_remedy_options(rec: Recall, remedy_cfg: Mapping[str, Any]) -> Recall:
    opts = normalize_remedy_options(rec, remedy_cfg)
    return rec if opts == rec.remedy_options else replace(rec, remedy_options=opts)


@dataclass(frozen=True)
class TierDecision:
    tier: str
    rule_id: str
    hold_days: Optional[int] = None
    hold_anchor: str = "published"


def _rule_applies(when: Mapping[str, Any], rec: Recall) -> bool:
    opts = set(rec.remedy_options)
    if "kind" in when and rec.kind not in set(when.get("kind") or []):
        return False
    if "remedy_options_any" in when and not (opts & set(when.get("remedy_options_any") or [])):
        return False
    if "remedy_options_none" in when and (opts & set(when.get("remedy_options_none") or [])):
        return False
    if "remedy_options_subset_of" in when and not opts <= set(
            when.get("remedy_options_subset_of") or []):
        return False
    if when.get("remedy_options_nonempty") and not opts:
        return False
    for key, value in (("title_regex", rec.title), ("remedy_regex", rec.remedy),
                       ("hazard_regex", rec.hazard)):
        pattern = when.get(key)
        if pattern and not re.search(pattern, norm_text(value)):
            return False
    return True


def decide_tier(rec: Recall, rules: Sequence[Mapping[str, Any]]) -> TierDecision:
    """The recall-level tier: the first rule whose ``when`` holds. Falls back
    to stop-use (the conservative tier) when no rule matches."""
    for rule in rules:
        if _rule_applies(rule.get("when") or {}, rec):
            tier = str(rule.get("tier") or TIER_STOP_USE)
            days = rule.get("hold_days")
            return TierDecision(tier=tier, rule_id=str(rule.get("id") or "rule"),
                                hold_days=int(days) if days is not None else None,
                                hold_anchor=str(rule.get("hold_anchor") or "published"))
    return TierDecision(tier=TIER_STOP_USE, rule_id="fallback")


def hold_until_for(decision: TierDecision, rec: Recall, *,
                   first_matched: Optional[_dt.datetime] = None) -> Optional[str]:
    """repair-hold end date: anchor "published" (the recall date: stock in
    the channel has been remedied for that long), "first_match", or "later"
    (whichever is later). None when the tier is not repair-hold or the anchor
    date is unknown (the hold then lasts until a human reviews it)."""
    if decision.tier != TIER_REPAIR_HOLD or decision.hold_days is None:
        return None
    published = _as_date(rec.published_at)
    matched = first_matched.date() if first_matched else None
    if decision.hold_anchor == "first_match":
        anchor = matched or published
    elif decision.hold_anchor == "later":
        anchor = max([d for d in (published, matched) if d], default=None)
    else:
        anchor = published
    if anchor is None:
        return None
    return (anchor + _dt.timedelta(days=decision.hold_days)).isoformat()


def is_blocking(tier: str, status: str, hold_until: Any, today: _dt.date) -> bool:
    """The buy-button rule the ``v_recall_buy_blocks`` view implements."""
    if status != STATUS_ACTIVE or tier not in BLOCKING_TIERS:
        return False
    if tier == TIER_REPAIR_HOLD:
        end = _as_date(hold_until)
        return end is None or end > today
    return True


# ---------------------------------------------------------------------------
# Site settings
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CategoryRule:
    key: str
    patterns: tuple[re.Pattern[str], ...]
    exclude: tuple[re.Pattern[str], ...] = ()
    catalog_categories: tuple[str, ...] = ()


def _phrases(values: Iterable[str]) -> tuple[tuple[str, ...], ...]:
    out: list[tuple[str, ...]] = []
    for v in values:
        toks = tokenize(v)
        if toks and toks not in out:
            out.append(toks)
    return tuple(out)


@dataclass(frozen=True)
class SiteSettings:
    """Everything the gate needs for one site, resolved from config."""

    site: str
    enabled: bool
    profile: str
    categories: tuple[CategoryRule, ...]
    relevance_fields: tuple[str, ...]
    generic_words: frozenset[str]
    variant_words: frozenset[str]
    brand_aliases: Mapping[str, tuple[str, ...]]
    max_span: int
    compat_lookback: int
    compat_cues: tuple[tuple[str, ...], ...]
    compat_reference_matches: str
    out_of_category: str
    min_model_number_len: int
    model_number_cues: frozenset[str]
    tier_rules: tuple[Mapping[str, Any], ...]
    remedy: Mapping[str, Any]
    dsn_env: str
    dsn_include_aliases: bool
    tables: Mapping[str, str]
    catalog_sql: str
    catalog_params: Mapping[str, Any]
    statement_timeout_ms: int
    connect_timeout_s: int
    post_sync_sql: tuple[str, ...]
    buy_button: Mapping[str, Any]
    email_enabled: bool
    exclusion_after: Optional[re.Pattern[str]] = None
    exclusion_cues: tuple[tuple[str, ...], ...] = ()
    unmapped_models: str = "review"

    def table(self, name: str) -> str:
        return _ident(self.tables.get(name) or DEFAULT_TABLES[name], f"{name} table")


def _storage_has(store: Any, key: str) -> bool:
    try:
        return bool(store.exists(key))
    except Exception:  # noqa: BLE001 - an unknown state is not proof of absence
        return True


def load_file_config(storage: Any = None, *, use_storage: bool = True,
                     repo_config: Optional[Path] = None) -> dict[str, Any]:
    """The storage copy of the config when present (it replaces the repo
    default wholesale), else the repo default file, else {}.

    Fails CLOSED: a storage copy that exists but cannot be read (an archived
    blob reads as None, invalid JSON reads as None) raises
    RecallGateConfigError instead of silently falling back to the repo
    default, whose by_site is empty (that would turn the gate off)."""
    if use_storage:
        store = storage
        if store is None:
            try:
                from framework.core.storage import get_storage
                store = get_storage()
            except (Exception, SystemExit) as e:  # get_storage exits on a bad backend
                log.warning("recall_gate: storage unavailable, repo config only: %s", e)
                store = None
        if store is not None:
            try:
                doc = store.read_json(CONFIG_KEY)
            except Exception as e:  # noqa: BLE001
                raise RecallGateConfigError(
                    f"storage copy of {CONFIG_KEY} could not be read ({type(e).__name__}); "
                    "refusing to fall back to the repo default") from None
            if isinstance(doc, dict) and doc:
                return doc
            if doc is None and _storage_has(store, CONFIG_KEY):
                raise RecallGateConfigError(
                    f"storage copy of {CONFIG_KEY} exists but could not be read (archived "
                    "blob or invalid JSON); refusing to fall back to the repo default")
            if doc is not None and not isinstance(doc, dict):
                raise RecallGateConfigError(f"storage copy of {CONFIG_KEY} is not a JSON object")
    path = Path(repo_config) if repo_config else REPO_CONFIG_PATH
    try:
        doc = json.loads(path.read_text())
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def resolve_config(file_cfg: Optional[Mapping[str, Any]] = None, *, storage: Any = None,
                   use_storage: bool = True) -> dict[str, Any]:
    """DEFAULTS <- the config file (storage copy or repo default)."""
    doc = dict(file_cfg) if file_cfg is not None else load_file_config(
        storage, use_storage=use_storage)
    cfg = _merge(DEFAULTS, doc)
    cfg["by_site"] = {k: v for k, v in (cfg.get("by_site") or {}).items()
                      if not str(k).startswith("_") and isinstance(v, Mapping)}
    cfg["profiles"] = {k: v for k, v in (cfg.get("profiles") or {}).items()
                       if not str(k).startswith("_") and isinstance(v, Mapping)}
    return cfg


def _compile_categories(raw: Mapping[str, Any], where: str,
                        problems: list[str]) -> tuple[CategoryRule, ...]:
    out: list[CategoryRule] = []
    for key, spec in (raw or {}).items():
        if str(key).startswith("_") or not isinstance(spec, Mapping):
            continue
        pats: list[re.Pattern[str]] = []
        excl: list[re.Pattern[str]] = []
        for bucket, target in (("patterns", pats), ("exclude", excl)):
            for p in spec.get(bucket) or []:
                try:
                    target.append(re.compile(str(p)))
                except re.error as e:
                    problems.append(f"{where}.categories.{key}.{bucket}: bad regex {p!r}: {e}")
        if not pats:
            problems.append(f"{where}.categories.{key}: no patterns")
            continue
        out.append(CategoryRule(
            key=str(key), patterns=tuple(pats), exclude=tuple(excl),
            catalog_categories=tuple(str(c) for c in (spec.get("catalog_categories") or [key]))))
    return tuple(out)


def site_settings(cfg: Mapping[str, Any], site: str) -> tuple[SiteSettings, list[str]]:
    """Resolve one ``by_site`` entry (merged over ``site_defaults`` and its
    profile). Returns (settings, problems); a site with problems must not be
    synced."""
    problems: list[str] = []
    entry = (cfg.get("by_site") or {}).get(site)
    if not isinstance(entry, Mapping):
        raise RecallGateConfigError(f"site {site!r} has no by_site entry")
    sc = _merge(cfg.get("site_defaults") or DEFAULTS["site_defaults"], entry)
    profile_name = str(sc.get("profile") or "")
    profile_doc = (cfg.get("profiles") or {}).get(profile_name) if profile_name else None
    if profile_name and not isinstance(profile_doc, Mapping):
        problems.append(f"by_site.{site}.profile: unknown profile {profile_name!r}")
    profile = dict(profile_doc) if isinstance(profile_doc, Mapping) else {}
    matching = _merge(cfg.get("matching") or DEFAULTS["matching"], profile.get("matching"))
    matching = _merge(matching, sc.get("matching"))
    cats_raw = sc.get("categories") if isinstance(sc.get("categories"), Mapping) else \
        profile.get("categories")
    categories = _compile_categories(cats_raw or {}, f"site {site}", problems)
    if not categories:
        problems.append(f"by_site.{site}: no categories (set a profile or categories)")
    generic = {str(w).lower() for w in DEFAULTS["matching"]["generic_words"]}
    for src in (matching.get("generic_words"), profile.get("generic_words"),
                sc.get("generic_words")):
        generic |= {str(w).lower() for w in (src or [])}
    variants: set[str] = set()
    for src in (matching.get("variant_words"), profile.get("variant_words"),
                sc.get("variant_words")):
        variants |= {str(w).lower() for w in (src or [])}
    aliases_raw = _merge(profile.get("brand_aliases") or {}, sc.get("brand_aliases") or {})
    brand_aliases = {norm_text(k): tuple(str(a) for a in (v or []))
                     for k, v in aliases_raw.items() if not str(k).startswith("_")}
    tiers = sc.get("tiers") or profile.get("tiers") or cfg.get("tiers") or DEFAULTS["tiers"]
    rules = tuple(r for r in (tiers.get("rules") or []) if isinstance(r, Mapping))
    for r in rules:
        if r.get("tier") not in BLOCKING_TIERS:
            problems.append(f"tier rule {r.get('id')!r}: tier must be one of {BLOCKING_TIERS}")
    if not rules:
        problems.append("no tier rules")
    tables = {k: str(v) for k, v in _merge(DEFAULT_TABLES, sc.get("tables")).items()}
    for k, v in tables.items():
        if not _IDENT_RE.match(v):
            problems.append(f"by_site.{site}.tables.{k}: unsafe identifier")
    catalog_sql = str(sc.get("catalog_sql") or "").strip()
    if sc.get("enabled") and not catalog_sql:
        problems.append(f"by_site.{site}.catalog_sql is required")
    if matching.get("compat_reference_matches") not in ("ignore", "review"):
        problems.append("matching.compat_reference_matches must be 'ignore' or 'review'")
    if matching.get("out_of_category") not in ("ignore", "review", "exact"):
        problems.append("matching.out_of_category must be 'ignore', 'review' or 'exact'")
    if matching.get("unmapped_models") not in ("ignore", "review"):
        problems.append("matching.unmapped_models must be 'ignore' or 'review'")
    exclusion_after: Optional[re.Pattern[str]] = None
    if matching.get("exclusion_after_regex"):
        try:
            exclusion_after = re.compile(str(matching["exclusion_after_regex"]))
        except re.error as e:
            problems.append(f"matching.exclusion_after_regex: bad regex: {e}")
    for marker in ((sc.get("buy_button") or {}).get("http") or {}).get("markers") or []:
        try:
            re.compile(str(marker))
        except re.error as e:
            problems.append(f"by_site.{site}.buy_button.http.markers: bad regex {marker!r}: {e}")
    rel_fields = tuple(str(f) for f in (sc.get("relevance_fields")
                                        or profile.get("relevance_fields")
                                        or ["title", "product_names"]))
    settings = SiteSettings(
        site=site, enabled=bool(sc.get("enabled")), profile=profile_name,
        categories=categories, relevance_fields=rel_fields,
        generic_words=frozenset(generic), variant_words=frozenset(variants),
        brand_aliases=brand_aliases,
        max_span=max(1, int(matching.get("max_span") or 8)),
        compat_lookback=max(0, int(matching.get("compat_lookback") or 0)),
        compat_cues=_phrases(matching.get("compat_cues") or []),
        compat_reference_matches=str(matching.get("compat_reference_matches") or "ignore"),
        out_of_category=str(matching.get("out_of_category") or "review"),
        min_model_number_len=max(3, int(matching.get("min_model_number_len") or 4)),
        model_number_cues=frozenset(str(c).lower() for c in (
            matching.get("model_number_cues") or [])),
        tier_rules=rules,
        remedy=_merge(DEFAULTS["remedy_options"], cfg.get("remedy_options")),
        dsn_env=str(sc.get("dsn_env") or ""),
        dsn_include_aliases=bool(sc.get("dsn_include_aliases")),
        tables=tables, catalog_sql=catalog_sql,
        catalog_params=dict(sc.get("catalog_params") or {}),
        statement_timeout_ms=int(sc.get("statement_timeout_ms") or 60000),
        connect_timeout_s=int(sc.get("connect_timeout_s") or 15),
        post_sync_sql=tuple(str(s) for s in (sc.get("post_sync_sql") or []) if str(s).strip()),
        buy_button=dict(sc.get("buy_button") or {}),
        email_enabled=bool((sc.get("email") or {}).get("enabled", True)),
        exclusion_after=exclusion_after,
        exclusion_cues=_phrases(matching.get("exclusion_cues") or []),
        unmapped_models=str(matching.get("unmapped_models") or "review"),
    )
    return settings, problems


def subscribed_sites(cfg: Mapping[str, Any]) -> list[str]:
    """Sites with ``by_site.<site>.enabled`` true, in config order."""
    out = []
    for site, entry in (cfg.get("by_site") or {}).items():
        merged = _merge(cfg.get("site_defaults") or DEFAULTS["site_defaults"], entry)
        if merged.get("enabled"):
            out.append(site)
    return out


def validate_config(cfg: Mapping[str, Any]) -> list[str]:
    """Problems with the global part of the config and with every enabled
    site. Messages name fields, never DSN values."""
    problems: list[str] = []
    for name in SOURCES:
        src = (cfg.get("sources") or {}).get(name) or {}
        if src.get("enabled") and not str(src.get("url") or "").startswith("https://"):
            problems.append(f"sources.{name}.url must be an https URL")
    for site in subscribed_sites(cfg):
        try:
            _, site_problems = site_settings(cfg, site)
        except RecallGateConfigError as e:
            site_problems = [str(e)]
        problems.extend(site_problems)
    return problems


def _site_registry(storage: Any = None, *, use_storage: bool = True, registry: Any = None) -> Any:
    """The effective site registry: built-in defaults merged with the storage
    override (a new site is usually registered ONLY in storage). With
    ``use_storage`` False, or ``registry`` given, storage is not touched."""
    if registry is not None:
        return registry
    from framework.core import site_registry  # local: keeps CLI start-up light
    return site_registry.load_registry(storage, use_storage=use_storage)


def resolve_dsn_env_names(settings: SiteSettings, *, storage: Any = None,
                          registry: Any = None, use_storage: bool = True) -> list[str]:
    """Env var NAMES that may hold the site's DSN, in lookup order: the
    site entry's ``dsn_env`` when set, else the site registry's ``dsn_env``
    (plus its aliases when ``dsn_include_aliases``). The registry is read
    with its storage override (``storage``, else the default backend)
    unless ``use_storage`` is False."""
    if settings.dsn_env:
        return [settings.dsn_env]
    reg = _site_registry(storage, use_storage=use_storage, registry=registry)
    entry = reg.get_site(settings.site)
    if entry is None:
        return []
    return list(entry.dsn_env_names(settings.dsn_include_aliases))


def site_hosts(settings: SiteSettings, *, storage: Any = None, registry: Any = None,
               use_storage: bool = True) -> list[str]:
    """Hosts the live-page probe may GET for this site: the site registry's
    domain and www.<domain>, plus ``buy_button.http.allowed_hosts``."""
    hosts: list[str] = []
    try:
        entry = _site_registry(storage, use_storage=use_storage,
                               registry=registry).get_site(settings.site)
    except Exception as e:  # noqa: BLE001 - the config list still applies
        log.warning("recall_gate: site registry unavailable for %s: %s", settings.site,
                    type(e).__name__)
        entry = None
    domain = str(getattr(entry, "domain", "") or "").strip().lower()
    if domain:
        hosts += [domain, domain if domain.startswith("www.") else f"www.{domain}"]
    for h in ((settings.buy_button or {}).get("http") or {}).get("allowed_hosts") or []:
        h = str(h).strip().lower()
        if h and h not in hosts:
            hosts.append(h)
    return list(dict.fromkeys(hosts))


def resolve_dsn(settings: SiteSettings, *, storage: Any = None, registry: Any = None,
                environ: Optional[Mapping[str, str]] = None,
                use_storage: bool = True) -> tuple[Optional[str], list[str]]:
    """(dsn value or None, candidate env var names). The value is returned,
    never logged."""
    env = os.environ if environ is None else environ
    names = resolve_dsn_env_names(settings, storage=storage, registry=registry,
                                  use_storage=use_storage)
    for n in names:
        if env.get(n):
            return env.get(n), names
    return None, names


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CatalogItem:
    """One row of a site's ``catalog_sql``. Only ``brand`` and one of
    ``product_id``/``model_id`` are required; every other column sharpens
    the match. Ids are opaque and written back as returned.

    Contract: catalog_sql returns every product whose page the site still
    renders (a retired or inactive product that keeps its page included). A
    blocked product it stops returning keeps its block, and the buy-button
    probe checks it at the page stored with its match (``item_locator``);
    with no stored page it is reported as unlisted, not as a live button.

    Amazon licence: every text column (brand, model_name, model_number,
    family, aliases, brand_aliases) must come from the site's OWN models /
    brands data, never from the Creators/PA-API offer cache. The gate keeps
    ``brand model_name`` forever (recall_matches.item_label, the evidence,
    the manual-queue mirror, emails), and the 24h purge only covers the
    offer cache. There is deliberately no free-text label column."""

    product_id: Any = None
    model_id: Any = None
    brand: str = ""
    model_name: str = ""
    model_number: str = ""
    family: str = ""
    aliases: tuple[str, ...] = ()
    brand_aliases: tuple[str, ...] = ()
    category: str = ""
    asin: str = ""
    upcs: tuple[str, ...] = ()
    components: tuple[str, ...] = ()
    url: str = ""
    slug: str = ""

    @property
    def key(self) -> str:
        return item_key(self.model_id, self.product_id)

    @property
    def display(self) -> str:
        name = " ".join(x for x in (self.brand, self.model_name) if x).strip()
        return name or self.model_number or self.key


def item_key(model_id: Any, product_id: Any) -> str:
    m = "" if model_id is None else str(model_id)
    p = "" if product_id is None else str(product_id)
    return f"m:{m}|p:{p}"


def match_key(rid: str, item: CatalogItem) -> str:
    return f"{rid}|{item.key}"


def catalog_from_rows(rows: Iterable[Mapping[str, Any]]) -> tuple[list[CatalogItem], list[str]]:
    """Rows (column name -> value) -> items. Rows without a brand or without
    any id are skipped and reported."""
    items: list[CatalogItem] = []
    problems: list[str] = []
    for i, row in enumerate(rows):
        r = {str(k).lower(): v for k, v in dict(row).items()}
        if r.get("product_id") is None and r.get("model_id") is None:
            problems.append(f"catalog row {i}: needs product_id or model_id")
            continue
        brand = clean_text(r.get("brand"))
        if not brand:
            problems.append(f"catalog row {i} ({item_key(r.get('model_id'), r.get('product_id'))}):"
                            " no brand")
            continue
        asin = clean_text(r.get("asin")).upper()
        items.append(CatalogItem(
            product_id=r.get("product_id"), model_id=r.get("model_id"), brand=brand,
            model_name=clean_text(r.get("model_name")),
            model_number=clean_text(r.get("model_number")),
            family=clean_text(r.get("family")),
            aliases=tuple(_as_list(r.get("aliases"))),
            brand_aliases=tuple(_as_list(r.get("brand_aliases"))),
            category=clean_text(r.get("category")),
            asin=asin if re.fullmatch(r"[A-Z0-9]{10}", asin) else "",
            upcs=tuple(re.sub(r"\D", "", u) for u in _as_list(r.get("upcs"))
                       if re.sub(r"\D", "", u)),
            components=tuple(_as_list(r.get("components"))),
            url=clean_text(r.get("url")), slug=clean_text(r.get("slug")),
        ))
    return items, problems


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

_ASIN_RE = re.compile(r"\b(B0[0-9A-Z]{8})\b")


@dataclass(frozen=True)
class _Field:
    name: str
    tokens: tuple[str, ...]
    company: bool = False
    # i in seps: a list separator (',', ';', '/') stands between token i and
    # token i+1. i in stops: a sentence or clause ends after token i.
    seps: frozenset[int] = frozenset()
    stops: frozenset[int] = frozenset()


# Fields that name what was recalled (as opposed to describing it at length).
_STRONG_FIELDS = frozenset({"title", "product_name", "product_model", "image_caption"})
_COUNT_BEFORE = frozenset({"recalls", "recall", "recalled", "about", "nearly", "approximately",
                           "over", "than", "some", "almost"})
_COUNT_AFTER = frozenset({"units", "unit", "million", "thousand", "pieces", "sets", "pairs"})


def _count_context(tokens: Sequence[str], s: int, e: int) -> bool:
    """The token span reads as a unit count ("About 25,030 units", "Recalls
    46,000 Heaters"), not as a model."""
    prev = tokens[s - 1] if s > 0 else ""
    nxt = tokens[e + 1] if e + 1 < len(tokens) else ""
    return prev in _COUNT_BEFORE or nxt in _COUNT_AFTER


class RecallView:
    """A recall prepared for matching: tokens per searchable field (built
    once per run and shared by every site), plus the ASINs, UPCs and model
    identifiers it names."""

    def __init__(self, rec: Recall):
        self.recall = rec
        fields: list[_Field] = []

        def add(name: str, value: str) -> None:
            toks, seps, stops = tokenize_with_gaps(value)
            if toks:
                fields.append(_Field(name, toks, seps=seps, stops=stops))
        add("title", rec.title)
        for v in rec.product_names:
            add("product_name", v)
        for v in rec.product_models:
            add("product_model", v)
        add("description", rec.description)
        if rec.announcement:
            add("announcement", rec.announcement)
        for v in rec.image_captions:
            add("image_caption", v)
        self.fields: tuple[_Field, ...] = tuple(fields)
        self.company_fields: tuple[_Field, ...] = tuple(
            _Field("company", toks, company=True)
            for toks in (tokenize(v) for v in rec.companies()) if toks)
        everything = " ".join([rec.title, rec.description, rec.announcement, rec.sold_at,
                               rec.remedy, rec.consumer_action, *rec.product_names,
                               *rec.product_models, *rec.image_captions]).upper()
        self.asins: frozenset[str] = frozenset(_ASIN_RE.findall(everything))
        self.upcs: frozenset[str] = frozenset(u.lstrip("0") for u in rec.upcs if u.lstrip("0"))
        ids = {t for f in self.fields for t in f.tokens if _is_identifier(t)}
        strong = {t for f in self.fields if f.name in _STRONG_FIELDS
                  for t in f.tokens if _is_identifier(t)}
        # A bare number in the title / product name / caption usually names
        # the model too ("Delta Max 2000", "HALO 1000"), unless it counts units
        # ("Recalls 46,000 Heaters").
        for f in self.fields:
            if f.name not in _STRONG_FIELDS:
                continue
            for i, t in enumerate(f.tokens):
                if t.isdigit() and 3 <= len(t) <= 5 and not _count_context(f.tokens, i, i):
                    ids.add(t)
                    strong.add(t)
        self.identifiers: frozenset[str] = frozenset(ids)
        # What the recall NAMES as models: identifiers in the title, product
        # names/models and captions. Specs in a description ("1800W AC
        # outlets", "LiFePO4 cells") do not name a model. Each site drops its
        # own generic words from this set.
        self.model_identifiers: frozenset[str] = frozenset(strong)
        self._index: dict[int, dict[str, list[tuple[int, int]]]] = {}
        self._stems: dict[int, dict[str, list[int]]] = {}
        self._compact_keys: Optional[frozenset[str]] = None

    def text_for(self, field_names: Sequence[str]) -> str:
        rec = self.recall
        parts: list[str] = []
        for name in field_names:
            if name == "title":
                parts.append(rec.title)
            elif name == "product_names":
                parts.extend(rec.product_names)
            elif name == "description":
                parts.append(rec.description)
            elif name == "image_captions":
                parts.extend(rec.image_captions)
        return norm_text(" | ".join(parts))

    def occurrences(self, field_idx: int) -> dict[str, list[tuple[int, int]]]:
        """compact form -> [(start, end)] for 1-3 adjacent tokens of a field
        (so "EFD 310" and "EFD-310" both find "efd310"). Never joined across
        a list separator or a sentence end."""
        idx = self._index.get(field_idx)
        if idx is None:
            f = self.fields[field_idx]
            toks = f.tokens
            idx = {}
            n = len(toks)
            for i in range(n):
                joined = ""
                for j in range(i, min(n, i + 3)):
                    if j > i and ((j - 1) in f.seps or (j - 1) in f.stops):
                        break
                    joined += toks[j]
                    idx.setdefault(joined, []).append((i, j))
            self._index[field_idx] = idx
        return idx

    def stem_positions(self, field_idx: int) -> dict[str, list[int]]:
        """number -> positions of the field's number+unit tokens ("2000w")."""
        st = self._stems.get(field_idx)
        if st is None:
            st = {}
            for i, t in enumerate(self.fields[field_idx].tokens):
                stem = _unit_stem(t)
                if stem is not None:
                    st.setdefault(stem, []).append(i)
            self._stems[field_idx] = st
        return st

    def compact_keys(self) -> frozenset[str]:
        """Every compact form of every field (for catalog-wide number lookups)."""
        if self._compact_keys is None:
            keys: set[str] = set()
            for fi in range(len(self.fields)):
                keys.update(self.occurrences(fi))
            self._compact_keys = frozenset(keys)
        return self._compact_keys


@dataclass(frozen=True)
class MatchResult:
    """One recall x catalog-row decision."""

    recall_id: str
    item: CatalogItem
    match_kind: str
    rule: str
    tier: str
    proposed_tier: str
    status: str
    hold_until: Optional[str]
    reason: str
    evidence: Mapping[str, Any]

    @property
    def key(self) -> str:
        return match_key(self.recall_id, self.item)

    @property
    def state_key(self) -> str:
        return f"{self.match_kind}/{self.tier}/{self.status}"

    def blocking(self, today: _dt.date) -> bool:
        return is_blocking(self.tier, self.status, self.hold_until, today)


@dataclass
class _Candidate:
    kind: str
    rule: str
    reason: str
    evidence: dict[str, Any]
    # (field index, covered token positions) of the clean windows behind a
    # model-name match; used to decide whether a longer catalog name
    # explains them.
    spans: tuple[tuple[int, frozenset[int]], ...] = ()


def _list_break_after(f: _Field, p: int) -> bool:
    """A list break follows token ``p`` of ``f``: a separator (',', ';',
    '/') after it, or 'and'/'or' as the token itself or the next one."""
    toks = f.tokens
    return (p in f.seps or toks[p] in _LIST_WORDS
            or (p + 1 < len(toks) and toks[p + 1] in _LIST_WORDS))


def _preceded_by_cue(tokens: Sequence[str], start: int, cues: Sequence[tuple[str, ...]],
                     lookback: int) -> bool:
    if lookback <= 0 or start <= 0:
        return False
    for cue in cues:
        n = len(cue)
        for end in range(max(0, start - lookback), start):
            st = end - n + 1
            if st >= 0 and tuple(tokens[st:end + 1]) == cue:
                return True
    return False


def _field_label(name: str) -> str:
    return name.replace("_", " ")


# Evidence that names the catalog row itself (an identifier), as opposed to
# its name: it stands on its own, whatever the recall's category.
_IDENTIFIER_RULES = frozenset({"asin", "upc", "model-number", "component-model-number",
                               "model-number-unnamed-brand"})


def _strong_global_number(c: str, generic: frozenset[str]) -> bool:
    """A model number distinctive enough to look up across the whole catalog
    without its brand: 5+ characters mixing letters and digits, and not a
    measurement ("20000mah") or a generic word ("lifepo4")."""
    return (len(c) >= 5 and _has_alpha(c) and _has_digit(c) and _unit_stem(c) is None
            and not _MEASURE_TOKEN_RE.match(c) and c not in generic)


class Matcher:
    """Deterministic recall -> catalog matcher for one site."""

    def __init__(self, settings: SiteSettings, items: Sequence[CatalogItem]):
        self.s = settings
        self.items = list(items)
        self._by_brand: dict[str, list[CatalogItem]] = {}
        self._brand_tokens: dict[str, set[str]] = {}
        self._brand_display: dict[str, str] = {}
        self._phrase_brand: dict[tuple[str, ...], set[str]] = {}
        self._first: dict[str, list[tuple[str, ...]]] = {}
        self._by_asin: dict[str, list[CatalogItem]] = {}
        self._by_upc: dict[str, list[CatalogItem]] = {}
        # compact model number -> brand keys of the catalog rows that carry it
        self._number_brands: dict[str, set[str]] = {}
        # strong compact number -> [(item, "model" | "component")]
        self._strong_index: dict[str, list[tuple[CatalogItem, str]]] = {}
        for it in self.items:
            bkey = norm_text(it.brand)
            self._by_brand.setdefault(bkey, []).append(it)
            self._brand_display.setdefault(bkey, it.brand)
            phrases = [it.brand, *it.brand_aliases, *settings.brand_aliases.get(bkey, ())]
            toks = self._brand_tokens.setdefault(bkey, set())
            for ph in _phrases(phrases):
                self._phrase_brand.setdefault(ph, set()).add(bkey)
                toks.update(ph)
            if it.asin:
                self._by_asin.setdefault(it.asin, []).append(it)
            for u in it.upcs:
                if u.lstrip("0"):
                    self._by_upc.setdefault(u.lstrip("0"), []).append(it)
            for mn in self.model_numbers(it):
                self._number_brands.setdefault(mn, set()).add(bkey)
                if _strong_global_number(mn, settings.generic_words):
                    self._strong_index.setdefault(mn, []).append((it, "model"))
            for comp in it.components:
                c = compact(comp)
                if c and _strong_global_number(c, settings.generic_words):
                    self._strong_index.setdefault(c, []).append((it, "component"))
        for ph in self._phrase_brand:
            self._first.setdefault(ph[0], []).append(ph)
        for lst in self._first.values():
            lst.sort(key=len, reverse=True)

    # -- relevance -----------------------------------------------------------

    def _subject_hit(self, pattern: re.Pattern[str], text: str) -> bool:
        """A pattern match that is not right after a compatibility cue:
        "Combiner Cables for Solar Panels" is a cable recall, not a panel one."""
        for m in pattern.finditer(text):
            before = _TOKEN_RE.findall(text[:m.start()])[-(self.s.compat_lookback + 3):]
            if not _preceded_by_cue(before, len(before), self.s.compat_cues,
                                    self.s.compat_lookback):
                return True
        return False

    def categories_for(self, view: RecallView) -> tuple[str, ...]:
        """The site categories a recall falls in (from its title and product
        names by default). Empty means the recall is outside the site's niche."""
        text = view.text_for(self.s.relevance_fields)
        out = []
        for rule in self.s.categories:
            if any(self._subject_hit(p, text) for p in rule.patterns) and not any(
                    x.search(text) for x in rule.exclude):
                out.append(rule.key)
        return tuple(out)

    def _catalog_categories(self, recall_categories: Sequence[str]) -> set[str]:
        out: set[str] = set()
        for rule in self.s.categories:
            if rule.key in recall_categories:
                out.update(rule.catalog_categories)
        return out

    def _in_cat(self, item: CatalogItem, recall_categories: Sequence[str]) -> bool:
        """The recall is about this row's kind of product (a row without a
        category is checked)."""
        return bool(recall_categories) and (
            not item.category or item.category in self._catalog_categories(recall_categories))

    # -- brands --------------------------------------------------------------

    def brands_in(self, view: RecallView) -> dict[str, dict[str, Any]]:
        """brand key -> {"subject": bool, "fields": [...]} for every catalog
        brand the recall names.

        A mention right after a compatibility cue ("for", "compatible with",
        "not a") is a reference. The title, product names/models, captions and
        company fields decide: the brand is the subject when one of them names
        it outside a cue (a company field always counts). Only when none of
        them mentions the brand at all does an unqualified mention in the
        description count. So "Replacement Battery Packs for Dyson Vacuums" is
        not a Dyson recall however often the description says "Dyson"."""
        hits: dict[str, dict[str, Any]] = {}
        for f in (*view.fields, *view.company_fields):
            strong = f.company or f.name in _STRONG_FIELDS
            toks = f.tokens
            for i, t in enumerate(toks):
                for ph in self._first.get(t, ()):
                    if tuple(toks[i:i + len(ph)]) != ph:
                        continue
                    ref = (not f.company) and _preceded_by_cue(
                        toks, i, self.s.compat_cues, self.s.compat_lookback)
                    for bkey in self._phrase_brand.get(ph, ()):
                        h = hits.setdefault(bkey, {"strong_subject": False, "strong_ref": False,
                                                   "weak_subject": False, "fields": []})
                        if strong:
                            h["strong_ref" if ref else "strong_subject"] = True
                        elif not ref:
                            h["weak_subject"] = True
                        if f.name not in h["fields"]:
                            h["fields"].append(f.name)
        for h in hits.values():
            h["subject"] = h["strong_subject"] or (not h["strong_ref"] and h["weak_subject"])
        return hits

    def _named_owner(self, number: str, brands: Optional[Mapping[str, Mapping[str, Any]]]
                     ) -> Optional[str]:
        """The display brand of a catalog row carrying model number
        ``number`` when the recall names that brand as its subject."""
        for b in sorted(self._number_brands.get(number, ())):
            if ((brands or {}).get(b) or {}).get("subject"):
                return self._brand_display.get(b, b)
        return None

    # -- item vocabulary -----------------------------------------------------

    def model_numbers(self, item: CatalogItem) -> list[str]:
        """Compact model numbers: the model_number column plus aliases that
        look like one (a digit, at most three tokens)."""
        out: list[str] = []
        for raw in [item.model_number, *item.aliases]:
            c = compact(raw)
            if not c or not _has_digit(c) or c in out:
                continue
            ntok = len(tokenize(raw))
            if raw != item.model_number and (ntok > 3 or (ntok > 1 and not _has_alpha(c))):
                continue
            out.append(c)
        return out

    def _name_variants(self, item: CatalogItem) -> list[str]:
        names = [item.model_name] if item.model_name else []
        for a in item.aliases:
            toks = tokenize(a)
            if len(toks) >= 2 or (toks and not _has_digit(toks[0])):
                names.append(a)
        return names

    def discriminators(self, item: CatalogItem, text: str) -> tuple[str, ...]:
        bt = self._brand_tokens.get(norm_text(item.brand), set())
        out: list[str] = []
        for t in tokenize(text):
            if t in self.s.generic_words or t in bt or t in out:
                continue
            out.append(t)
        return tuple(out)

    def family_tokens(self, item: CatalogItem) -> tuple[str, ...]:
        if item.family:
            return self.discriminators(item, item.family)
        return tuple(t for t in self.discriminators(item, item.model_name)
                     if not _has_digit(t) and t not in self.s.variant_words)

    def _has_detail(self, item: CatalogItem) -> bool:
        return bool(self.model_numbers(item)) or any(
            self.discriminators(item, n) for n in self._name_variants(item))

    def _numbered_only(self, item: CatalogItem) -> bool:
        """The catalog name distinguishes the row by a number alone
        ("300W Portable Power Station", "HALO 1000")."""
        toks = [t for n in self._name_variants(item) for t in self.discriminators(item, n)]
        return bool(toks) and all(_numeric_like(t) for t in toks)

    def _strong_model_number(self, mn: str) -> bool:
        if not _has_digit(mn):
            return False
        if _has_alpha(mn):
            return len(mn) >= 3
        return True  # pure digits: _find_number demands length or a "model" cue

    # -- windows -------------------------------------------------------------

    def _windows(self, view: RecallView, field_idx: int, wanted: Sequence[str]
                 ) -> list[tuple[int, int, frozenset[int], bool]]:
        """(start, end, covered, via_unit) for every window of at most
        max_span tokens that holds all ``wanted`` tokens. A bare catalog number
        also matches the recall's number+unit token ("2000" ~ "2000w") and a
        catalog number+unit the recall's bare number ("500w" ~ "500");
        ``via_unit`` says one did.

        Each window takes one occurrence per wanted token, picked twice over:
        anchored on a start (each token's earliest-ending occurrence after
        it) and anchored on an end (each token's occurrence nearest before
        it). The second finds a model named in an ELIDED list, where the
        words the items share stand once, before the first item: "Explorer
        1000 and 1000 Plus" names "Explorer 1000 Plus", "Prowler Pro / Pro
        Crew" names "Prowler Pro Crew", "Explorer 1000 Pro and 2000 Pro"
        names "Explorer 2000 Pro".

        Only minimal windows are kept: another, uncovered occurrence of a
        wanted token inside the window means it spans two mentions ("Q20 and
        Q20 Pro" is not one mention of "Q20 Pro"; the tighter window is found
        instead). The one exception is the elided-list shape, all of:

        * every such occurrence sits in ONE gap between two covered tokens;
        * the covered token before the gap runs straight into it (no list
          break or sentence end after it): it is the shared prefix, not an
          item of its own;
        * the gap is whole list items: it ends with a list break, holds no
          sentence end, and each item starts or ends with a word of the item
          after the gap (the items are parallel);
        * the covered words read in the model name's order, so the ones
          before the gap are its LEADING words (an elided list states the
          shared words first).

        So "Explorer [1000 and] 1000 Plus" is kept, while "EU2200i, [EU2200i
        Companion and] EB2200i" never names an "EU2200i EB2200i", "RANGER
        CREW [1000,] RANGER XP 1000" never names a "RANGER XP 1000 Crew", and
        "S32 [Triathlon Bicycles and 2010 Felt S32,] B12" never names an
        "S32 B12". An occurrence counts however it was found: a joined
        "EFD 310" or a "2000W" for a wanted "2000" is another mention too."""
        occ = view.occurrences(field_idx)
        stems = view.stem_positions(field_idx)
        lists: dict[str, list[tuple[int, int, bool]]] = {}
        for w in set(wanted):
            found = [(a, b, False) for (a, b) in occ.get(w, ())]
            if w.isdigit():
                found += [(p, p, True) for p in stems.get(w, ())]
            else:
                stem = _unit_stem(w)
                if stem is not None:
                    found += [(a, b, True) for (a, b) in occ.get(stem, ()) if a == b]
            if not found:
                return []
            lists[w] = found
        f = view.fields[field_idx]
        span = self.s.max_span
        rank = {w: i for i, w in enumerate(dict.fromkeys(wanted))}
        out: set[tuple[int, int, frozenset[int], bool]] = set()

        def keep(chosen: Mapping[str, tuple[int, int, bool]]) -> None:
            covered = frozenset(p for (a, b, _) in chosen.values() for p in range(a, b + 1))
            s, e = min(covered), max(covered)
            gaps: set[tuple[int, int]] = set()
            for lst in lists.values():
                for (a, b, _) in lst:
                    if a < s or b > e or any(p in covered for p in range(a, b + 1)):
                        continue
                    gaps.add((max(c for c in covered if c < a), min(c for c in covered if c > b)))
            if gaps:
                if len(gaps) > 1:
                    return  # skipped occurrences in two places: not one mention
                (before, after) = next(iter(gaps))
                if (_list_break_after(f, before) or not _list_break_after(f, after - 1)
                        or any(q in f.stops for q in range(before, after))):
                    return  # not "<prefix> <whole list items> <item>"
                order = [rank[w] for w, _ in sorted(chosen.items(),
                                                    key=lambda kv: (kv[1][0], rank[kv[0]]))]
                if order != sorted(order):
                    return  # not in the name's word order: not its leading words
                rest = [w for w, (a, _, _) in chosen.items() if a >= after]
                firsts = {a for w in rest for (a, _, _) in lists[w]}
                lasts = {b for w in rest for (_, b, _) in lists[w]}
                item: list[int] = []
                for q in range(before + 1, after):
                    if f.tokens[q] not in _LIST_WORDS:
                        item.append(q)
                    if item and (f.tokens[q] in _LIST_WORDS or q in f.seps or q == after - 1):
                        if item[0] not in firsts and item[-1] not in lasts:
                            return  # a skipped item unlike the named one
                        item = []
            out.add((s, e, covered, any(unit for (_, _, unit) in chosen.values())))

        for s in sorted({a for v in lists.values() for (a, _, _) in v}):
            limit = s + span - 1
            chosen: dict[str, tuple[int, int, bool]] = {}
            for w, lst in lists.items():
                cands = [x for x in lst if x[0] >= s and x[1] <= limit]
                if not cands:
                    break
                chosen[w] = min(cands, key=lambda x: (x[1], x[0], x[2]))
            else:
                if min(a for (a, _, _) in chosen.values()) == s:
                    keep(chosen)
        for e in sorted({b for v in lists.values() for (_, b, _) in v}):
            floor = e - span + 1
            chosen = {}
            for w, lst in lists.items():
                cands = [x for x in lst if x[1] <= e and x[0] >= floor]
                if not cands:
                    break
                chosen[w] = max(cands, key=lambda x: (x[1], x[0], not x[2]))
            else:
                if max(b for (_, b, _) in chosen.values()) == e:
                    keep(chosen)
        return sorted(out, key=lambda w: (w[0], w[1], w[3], sorted(w[2])))

    def _variant_conflict(self, f: _Field, s: int, e: int, covered: frozenset[int],
                          wanted: set[str], numbers: set[str]) -> Optional[str]:
        """A token inside or right next to the window that makes the recalled
        product a different variant: a variant word ("pro", "max") or a short
        number ("2000", "v2"). Long alphanumerics are model numbers, not
        variants ("Seagull Pro ZT6001"), and a number with a unit is a rating
        ("C1000 1056Wh"). A token that a list separator divides from the
        window is another list item, not a variant: "Explorer 1000, 1500 and
        2000" names all three, "Q20 and Q20 Pro" names both, and so do
        "Explorer 1000 and 1000 Plus" and "Explorer 1000 Pro and 2000 Pro"."""
        toks, seps = f.tokens, f.seps
        n = len(toks)

        def numberish(t: str) -> bool:
            return (_has_digit(t) and _unit_stem(t) is None
                    and (t.isdigit() or len(t) <= 4) and t not in numbers)

        cov = sorted(covered)
        for pos in range(s, e + 1):
            if pos in covered:
                continue
            nxt = next((c for c in cov if c > pos), e)
            if any(_list_break_after(f, q) for q in range(pos, nxt)):
                continue
            t = toks[pos]
            if t in self.s.variant_words or numberish(t):
                return t
        if e + 1 < n and e not in seps:
            t = toks[e + 1]
            if t not in wanted and (t in self.s.variant_words or numberish(t)):
                return t
        if s - 1 >= 0 and (s - 1) not in seps:
            t = toks[s - 1]
            if t not in wanted and t in self.s.variant_words:
                return t
        return None

    def _excluded(self, f: _Field, s: int, e: int) -> bool:
        """The recall scopes this mention OUT: "... is not included in this
        recall" after it in the same sentence, or "excluding" right before."""
        if self.s.exclusion_cues and _preceded_by_cue(
                f.tokens, s, self.s.exclusion_cues, max(1, self.s.compat_lookback)):
            return True
        rx = self.s.exclusion_after
        if rx is None:
            return False
        after: list[str] = []
        for j in range(e + 1, min(len(f.tokens), e + 13)):
            if (j - 1) in f.stops:
                break
            after.append(f.tokens[j])
        return bool(after) and rx.match(" ".join(after)) is not None

    def _find_number(self, view: RecallView, mn: str) -> list[dict[str, Any]]:
        hits = []
        digits = not _has_alpha(mn)
        short_digits = digits and len(mn) < max(5, self.s.min_model_number_len)
        for fi, f in enumerate(view.fields):
            for (s, e) in view.occurrences(fi).get(mn, []):
                if digits and _count_context(f.tokens, s, e):
                    continue  # "About 25,030 units": a unit count, not a model
                if short_digits and not set(f.tokens[max(0, s - 3):s]) & self.s.model_number_cues:
                    continue
                excluded = self._excluded(f, s, e)
                ref = excluded or _preceded_by_cue(f.tokens, s, self.s.compat_cues,
                                                   self.s.compat_lookback)
                hits.append({"field": f.name, "reference": ref, "excluded": excluded,
                             "text": " ".join(f.tokens[max(0, s - 3):e + 4])})
        return hits

    def _find_name(self, view: RecallView, wanted: tuple[str, ...], numbers: set[str]
                   ) -> list[dict[str, Any]]:
        hits = []
        wset = set(wanted)
        for fi, f in enumerate(view.fields):
            toks = f.tokens
            for (s, e, covered, via_unit) in self._windows(view, fi, wanted):
                if all(toks[p].isdigit() for p in covered) and _count_context(toks, s, e):
                    continue  # "About 1,000 units" does not name the "HALO 1000"
                excluded = self._excluded(f, s, e)
                ref = excluded or _preceded_by_cue(toks, s, self.s.compat_cues,
                                                   self.s.compat_lookback)
                conflict = self._variant_conflict(f, s, e, covered, wset, numbers)
                # A name made only of a number, found as a rating in running
                # text ("... a 1000W AC outlet"), is weaker than a named model.
                rating = (via_unit and f.name not in _STRONG_FIELDS and not any(
                    _has_alpha(toks[p]) and _unit_stem(toks[p]) is None for p in covered))
                hits.append({"field": f.name, "reference": ref, "excluded": excluded,
                             "conflict": conflict, "rating": rating, "span": (fi, s, e),
                             "covered": covered,
                             "text": " ".join(toks[max(0, s - 2):e + 3])})
        return hits

    # -- classification ------------------------------------------------------

    def classify(self, view: RecallView, item: CatalogItem, *, brand_hit: Optional[Mapping],
                 relevant_categories: Sequence[str],
                 brands: Optional[Mapping[str, Mapping[str, Any]]] = None
                 ) -> Optional[_Candidate]:
        """The strongest evidence that ``item`` is covered by the recall."""
        brand_subject = bool(brand_hit and brand_hit.get("subject"))
        brand_ev: dict[str, Any] = {
            "brand": item.brand, "brand_fields": sorted((brand_hit or {}).get("fields") or []),
            "brand_subject": brand_subject}
        best: Optional[_Candidate] = None
        excluded = False

        def consider(c: _Candidate) -> None:
            nonlocal best
            if best is None or _KIND_RANK[c.kind] > _KIND_RANK[best.kind]:
                best = c

        numbers = set(self.model_numbers(item))
        # 1. model numbers, then kit components
        for mn in sorted(numbers):
            if not self._strong_model_number(mn):
                continue
            hits = self._find_number(view, mn)
            if not hits:
                continue
            subj = [h for h in hits if not h["reference"]]
            h = (subj or hits)[0]
            ev = {**brand_ev, "model_number": mn, "field": h["field"], "context": h["text"]}
            if subj and brand_subject:
                return _Candidate(MATCH_EXACT, "model-number",
                                  f"model number {mn.upper()} appears in the recall "
                                  f"{_field_label(h['field'])} and {item.brand} is named as "
                                  "the recalled product's brand", ev)
            if not subj and all(x["excluded"] for x in hits):
                excluded = True
                consider(_Candidate(MATCH_AMBIGUOUS, "model-number-excluded",
                                    f"model number {mn.upper()} appears only where the recall "
                                    "says it is not covered", ev))
                continue
            consider(_Candidate(
                MATCH_AMBIGUOUS, "model-number-reference",
                f"model number {mn.upper()} appears only "
                + ("as a compatibility reference" if not subj
                   else f"while {item.brand} is named only as a compatibility reference"),
                ev))
        for comp in item.components:
            c = compact(comp)
            if not c or not self._strong_model_number(c):
                continue
            hits = [h for h in self._find_number(view, c) if not h["reference"]]
            if not hits:
                continue
            owner = None if brand_subject else self._named_owner(c, brands)
            if brand_subject or owner:
                ev = {**brand_ev, "component": c, "field": hits[0]["field"],
                      "context": hits[0]["text"]}
                if owner:
                    ev["component_brand"] = owner
                return _Candidate(MATCH_EXACT, "component-model-number",
                                  f"kit component model number {c.upper()} appears in the "
                                  f"recall {_field_label(hits[0]['field'])}"
                                  + (f" and the component's brand {owner} is the recalled "
                                     "brand" if owner else ""), ev)
        if not brand_hit:
            return best
        # 2. model-name windows
        for name in self._name_variants(item):
            wanted = self.discriminators(item, name)
            if not wanted:
                continue
            hits = self._find_name(view, wanted, numbers)
            if not hits:
                continue
            ev = {**brand_ev, "name": name, "tokens": list(wanted)}
            live = [h for h in hits if not h["reference"]]
            clean = [h for h in live if not h["conflict"] and not h["rating"]]
            if clean and brand_subject and excluded:
                # The recall names this row's own model number only to scope
                # it out: a shared model name does not overrule that (review).
                continue
            if clean and brand_subject:
                h = clean[0]
                return _Candidate(MATCH_EXACT, "model-name",
                                  f"every distinguishing token of '{name}' "
                                  f"({' '.join(wanted)}) appears together in the recall "
                                  f"{_field_label(h['field'])}",
                                  {**ev, "field": h["field"], "context": h["text"]},
                                  spans=tuple((x["span"][0], x["covered"]) for x in clean))
            conflicted = [h for h in live if h["conflict"]]
            rated = [h for h in live if h["rating"] and not h["conflict"]]
            if conflicted and brand_subject:
                h = conflicted[0]
                consider(_Candidate(MATCH_SIBLING, "variant",
                                    f"'{name}' appears next to '{h['conflict']}' in the recall "
                                    f"{_field_label(h['field'])}: the recall names a "
                                    "different variant",
                                    {**ev, "field": h["field"], "context": h["text"],
                                     "variant": h["conflict"]}))
            elif rated and brand_subject:
                h = rated[0]
                consider(_Candidate(MATCH_AMBIGUOUS, "model-name-rating",
                                    f"'{name}' is a number the recall "
                                    f"{_field_label(h['field'])} gives only as a rating, not "
                                    "as a model name",
                                    {**ev, "field": h["field"], "context": h["text"]}))
            elif brand_subject and all(x["excluded"] for x in hits):
                excluded = True
                h = hits[0]
                consider(_Candidate(MATCH_AMBIGUOUS, "model-name-excluded",
                                    f"'{name}' appears only where the recall says it is not "
                                    "covered",
                                    {**ev, "field": h["field"], "context": h["text"]}))
            else:
                h = hits[0]
                consider(_Candidate(
                    MATCH_AMBIGUOUS, "model-name-reference",
                    f"'{name}' appears only "
                    + ("as a compatibility reference" if h["reference"]
                       else f"while {item.brand} is named only as a compatibility reference"),
                    {**ev, "field": h["field"], "context": h["text"]}))
        # Weaker evidence (same line, or the brand alone) only counts when the
        # recall is about the item's kind of product: a Yeti cable recall says
        # nothing about Yeti stations. A row the recall explicitly scopes out
        # gets no weaker evidence either.
        if not brand_subject or excluded or not self._in_cat(item, relevant_categories):
            return best
        # 3. family / product line
        fam = self.family_tokens(item)
        if fam:
            hits = [h for h in self._find_name(view, fam, numbers) if not h["reference"]]
            if hits:
                h = hits[0]
                consider(_Candidate(MATCH_SIBLING, "family",
                                    f"same product line ({' '.join(fam)}) as the recalled "
                                    "product, different model",
                                    {**brand_ev, "family": list(fam), "field": h["field"],
                                     "context": h["text"]}))
        # 4. nothing to compare
        if not self._has_detail(item):
            consider(_Candidate(MATCH_AMBIGUOUS, "no-model-detail",
                                "the catalog row has no model name or number to compare; "
                                f"the recall covers {item.brand} products in this category",
                                {**brand_ev, "categories": list(relevant_categories)}))
        return best

    @staticmethod
    def _supersede(exact_names: Sequence[tuple[CatalogItem, set[str],
                                               tuple[tuple[int, frozenset[int]], ...]]],
                   cands: dict[str, tuple[CatalogItem, _Candidate]]) -> None:
        """Maximal munch, by position: a catalog name whose tokens are a
        strict subset of another's ("HALO 1000" vs "HALO 1000 Bolt") is a
        different variant only when EVERY place the recall names it is part
        of a place the recall names the longer one (its tokens are tokens of
        that mention). "Ridstar Q20 and Q20 Pro" names both, and so do
        "Explorer 1000 and 1000 Plus" and "Prowler Pro / Pro Crew": the
        shorter name's own item is a separate list item, not a part of the
        longer mention that shares its first words."""
        for it, toks, spans in exact_names:
            longer = [sp for (o, otoks, ospans) in exact_names
                      if o.key != it.key and toks < otoks for sp in ospans]
            if not longer or not spans:
                continue
            if all(any(fi == lf and cov <= lcov for (lf, lcov) in longer)
                   for (fi, cov) in spans):
                c = cands[it.key][1]
                cands[it.key] = (it, _Candidate(
                    MATCH_SIBLING, "superseded-by-longer-name",
                    "every place the recall names this model is part of a longer catalog "
                    "model name, so the recall names that variant", dict(c.evidence)))

    def _brand_level(self, view: RecallView, hit: Mapping[str, Any],
                     brand_items: Sequence[CatalogItem], cats: Sequence[str],
                     cands: dict[str, tuple[CatalogItem, _Candidate]]) -> None:
        """In-category rows of a brand the recall is about that nothing above
        matched (the renamed-model safety net):

        * the recall maps to none of this site's models of the brand and
          names no model either (only generic words or description specs)
          -> ambiguous "brand-wide";
        * it maps to none of them but names models -> ambiguous
          "unmapped-model" (``matching.unmapped_models``);
        * it maps to some, names numbered models ("400W and 500W"), and this
          row's name is a number alone ("300W ...") -> sibling
          "numbered-variant". Other rows of the brand are not queued: the
          recall named which models it covers."""
        if not cats:
            return
        todo = [it for it in brand_items
                if it.key not in cands and self._in_cat(it, cats) and self._has_detail(it)]
        if not todo:
            return
        mapped = any(it.key in cands and cands[it.key][1].kind in (MATCH_EXACT, MATCH_SIBLING)
                     for it in brand_items)
        ids = sorted(t for t in view.model_identifiers if t not in self.s.generic_words)
        numeric = [t for t in ids if _numeric_like(t)]
        for it in todo:
            ev: dict[str, Any] = {"brand": it.brand,
                                  "brand_fields": sorted(hit.get("fields") or []),
                                  "brand_subject": True, "categories": list(cats)}
            if not mapped and not ids:
                c = _Candidate(MATCH_AMBIGUOUS, "brand-wide",
                               f"the recall names {it.brand} products in this category but no "
                               "model, so it may cover this one", ev)
            elif not mapped:
                if self.s.unmapped_models != "review":
                    continue
                c = _Candidate(MATCH_AMBIGUOUS, "unmapped-model",
                               f"the recall names {', '.join(t.upper() for t in ids[:5])}, "
                               f"which matches none of this site's {it.brand} models; it may be "
                               "this one under another name",
                               {**ev, "recall_identifiers": ids[:10]})
            elif numeric and self._numbered_only(it):
                c = _Candidate(MATCH_SIBLING, "numbered-variant",
                               "same brand and category, and this model's name differs from "
                               f"the recalled {', '.join(t.upper() for t in numeric[:5])} only "
                               "by its number", {**ev, "recall_identifiers": numeric[:10]})
            else:
                continue
            cands[it.key] = (it, c)

    def _unnamed_brand_numbers(self, view: RecallView,
                               brands: Mapping[str, Mapping[str, Any]],
                               cands: dict[str, tuple[CatalogItem, _Candidate]]) -> None:
        """A strong model number (5+ letters and digits) of a catalog row
        whose brand the recall does not name (a sub-brand like "Anker SOLIX",
        a legal name, a missing brand alias) is still evidence: ambiguous, for
        a human. A kit's component number is exact when the component's own
        brand is the recalled brand."""
        if not self._strong_index:
            return
        for n in sorted(view.compact_keys() & self._strong_index.keys()):
            hits = [h for h in self._find_number(view, n) if not h["reference"]]
            if not hits:
                continue
            h = hits[0]
            for it, role in self._strong_index[n]:
                if norm_text(it.brand) in brands:
                    continue  # the brand pass decided this row (or ignored it on purpose)
                prev = cands.get(it.key)
                if prev is not None and prev[1].kind == MATCH_EXACT:
                    continue
                owner = self._named_owner(n, brands) if role == "component" else None
                what = "kit component model number" if role == "component" else "model number"
                ev = {"brand": it.brand, "brand_named": False,
                      ("component" if role == "component" else "model_number"): n,
                      "field": h["field"], "context": h["text"]}
                if owner:
                    c = _Candidate(MATCH_EXACT, "component-model-number",
                                   f"{what} {n.upper()} appears in the recall "
                                   f"{_field_label(h['field'])} and the component's brand "
                                   f"{owner} is the recalled brand",
                                   {**ev, "component_brand": owner})
                else:
                    c = _Candidate(MATCH_AMBIGUOUS, "model-number-unnamed-brand",
                                   f"{what} {n.upper()} appears in the recall "
                                   f"{_field_label(h['field'])}, which does not name "
                                   f"{it.brand} as the recalled brand (a sub-brand, a legal "
                                   "name or a model-number clash): check it", ev)
                if prev is None or _KIND_RANK[c.kind] > _KIND_RANK[prev[1].kind]:
                    cands[it.key] = (it, c)

    def match(self, view: RecallView, *, decision: Optional[TierDecision] = None,
              first_matched: Optional[Mapping[str, Optional[_dt.datetime]]] = None,
              now: Optional[_dt.datetime] = None
              ) -> tuple[tuple[str, ...], list[MatchResult]]:
        """(recall categories for this site, matches) for one recall.

        ``first_matched`` maps match keys to when each was first matched (the
        anchor of a "first_match"/"later" repair hold); a key not in it is
        matched for the first time ``now``."""
        rec = view.recall
        cats = self.categories_for(view)
        decision = decision or decide_tier(rec, self.s.tier_rules)
        cands: dict[str, tuple[CatalogItem, _Candidate]] = {}
        brands = self.brands_in(view)

        # An identifier the CPSC printed is exact even when the recall does not
        # name the catalog brand (CPSC has listed one ASIN under two different
        # products: a repurposed listing still sold the recalled one). The
        # evidence says so, and the email asks for a look.
        for rule, ids, index in (("asin", view.asins, self._by_asin),
                                 ("upc", view.upcs, self._by_upc)):
            for ident in sorted(ids):
                for it in index.get(ident, []):
                    if it.key in cands:
                        continue
                    named = bool((brands.get(norm_text(it.brand)) or {}).get("subject"))
                    what = "listing's ASIN" if rule == "asin" else "product's UPC"
                    reason = f"the recall names this {what} {ident}"
                    if not named:
                        reason += (f"; it does not name {it.brand} as the recalled brand, so "
                                   "the listing may have been repurposed: check it")
                    cands[it.key] = (it, _Candidate(MATCH_EXACT, rule, reason,
                                                    {rule: ident, "brand": it.brand,
                                                     "brand_named": named}))

        for bkey in sorted(brands):
            hit = brands[bkey]
            if not hit["subject"] and self.s.compat_reference_matches == "ignore":
                continue
            brand_items = self._by_brand.get(bkey, [])
            exact_names: list[tuple[CatalogItem, set[str],
                                    tuple[tuple[int, frozenset[int]], ...]]] = []
            for it in brand_items:
                if it.key in cands and cands[it.key][1].kind == MATCH_EXACT:
                    continue
                c = self.classify(view, it, brand_hit=hit, relevant_categories=cats,
                                  brands=brands)
                if c is None:
                    continue
                prev = cands.get(it.key)
                if prev is None or _KIND_RANK[c.kind] > _KIND_RANK[prev[1].kind]:
                    cands[it.key] = (it, c)
                if c.kind == MATCH_EXACT and c.rule == "model-name":
                    exact_names.append((it, set(c.evidence.get("tokens") or []), c.spans))
            self._supersede(exact_names, cands)
            if hit["subject"]:
                self._brand_level(view, hit, brand_items, cats, cands)
        self._unnamed_brand_numbers(view, brands, cands)

        results: list[MatchResult] = []
        fm = first_matched or {}
        anchor_now = now or _utcnow()
        for key in sorted(cands):
            it, c = cands[key]
            kind, rule, reason = c.kind, c.rule, c.reason
            if not cats and rule not in _IDENTIFIER_RULES:
                if kind != MATCH_EXACT or self.s.out_of_category == "ignore":
                    continue
                if self.s.out_of_category == "review":
                    kind, rule = MATCH_AMBIGUOUS, "out-of-category"
                    reason = (f"{c.reason}; but the recall is outside this site's "
                              "configured categories")
            evidence = {**c.evidence, "rule": rule, "matcher_version": MATCHER_VERSION,
                        "recall_categories": list(cats), "tier_rule": decision.rule_id}
            mk = match_key(rec.id, it)
            if kind == MATCH_EXACT:
                tier, status = decision.tier, STATUS_ACTIVE
                hold = hold_until_for(decision, rec, first_matched=fm.get(mk) or anchor_now)
            else:
                tier, status, hold = TIER_REVIEW, STATUS_REVIEW, None
            results.append(MatchResult(
                recall_id=rec.id, item=it, match_kind=kind, rule=rule, tier=tier,
                proposed_tier=decision.tier, status=status, hold_until=hold,
                reason=reason[:1000], evidence=evidence))
        return cats, results


# ---------------------------------------------------------------------------
# HTTP sources
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


HttpGet = Callable[..., HttpResponse]


class _Retryable(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Leave a 3xx to the caller (it comes back as a response)."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any,
                         newurl: str) -> None:
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect)


def urllib_get(url: str, *, headers: Mapping[str, str], timeout: float,
               follow_redirects: bool = True) -> HttpResponse:
    """GET with gzip support. 304 and 4xx/5xx come back as responses, and so
    does a 3xx when ``follow_redirects`` is False (Location in the headers)."""
    req = urllib.request.Request(url, headers=dict(headers), method="GET")
    opener: Callable[..., Any] = (urllib.request.urlopen if follow_redirects
                                  else _NO_REDIRECT_OPENER.open)
    try:
        with opener(req, timeout=timeout) as r:  # noqa: S310 - https only
            status = int(getattr(r, "status", 200) or 200)
            hdrs = {k.lower(): v for k, v in r.headers.items()}
            body = r.read()
    except urllib.error.HTTPError as e:
        hdrs = {k.lower(): v for k, v in (e.headers or {}).items()}
        try:
            body = e.read() or b""
        except Exception:  # noqa: BLE001
            body = b""
        status = int(e.code)
    if hdrs.get("content-encoding", "").lower() == "gzip" and body:
        body = gzip.decompress(body)
    return HttpResponse(status=status, headers=hdrs, body=body)


def _get_with_retry(http_get: HttpGet, url: str, *, headers: Mapping[str, str],
                    timeout: float, retries: int, base_delay: float) -> HttpResponse:
    from framework.core.resilience import with_retry

    @with_retry(retries=max(0, retries), base_delay=base_delay, backoff=2.0,
                max_delay=60.0, on=(_Retryable, urllib.error.URLError, TimeoutError,
                                    ConnectionError))
    def _do() -> HttpResponse:
        resp = http_get(url, headers=headers, timeout=timeout)
        if resp.status >= 500 or resp.status == 429:
            raise _Retryable(f"HTTP {resp.status} from {urllib.parse.urlsplit(url).netloc}")
        return resp
    return _do()


@dataclass
class SourceResult:
    name: str
    ok: bool
    records: list[Recall] = field(default_factory=list)
    not_modified: bool = False
    error: str = ""
    state: dict[str, Any] = field(default_factory=dict)
    detail: dict[str, Any] = field(default_factory=dict)


class SourceFetcher:
    """Fetches and parses the CPSC sources. ``http_get`` is injectable
    (tests pass fixture-backed fakes); the default is ``urllib_get``."""

    def __init__(self, cfg: Mapping[str, Any], *, http_get: Optional[HttpGet] = None,
                 now: Optional[_dt.datetime] = None, cache_dir: Optional[str] = None):
        self.cfg = cfg
        self.http_get: HttpGet = http_get or urllib_get
        self.now = now or _utcnow()
        http_cfg = cfg.get("http") or {}
        self.user_agent = str(http_cfg.get("user_agent") or DEFAULTS["http"]["user_agent"])
        self.retries = int(http_cfg.get("retries", 2))
        self.base_delay = float(http_cfg.get("retry_base_delay_s", 2.0))
        raw_cache = cache_dir if cache_dir is not None else str(http_cfg.get("cache_dir") or "")
        self.cache_dir: Optional[Path] = Path(os.path.expanduser(raw_cache)) if raw_cache else None

    def _src(self, name: str) -> Mapping[str, Any]:
        src = (self.cfg.get("sources") or {}).get(name) or {}
        if not str(src.get("url") or "").startswith("https://"):
            raise SourceError(f"{name}: source url must be https")
        return src

    def enabled(self) -> list[str]:
        return [n for n in SOURCES
                if ((self.cfg.get("sources") or {}).get(n) or {}).get("enabled")]

    def _headers(self, accept: str) -> dict[str, str]:
        return {"User-Agent": self.user_agent, "Accept": accept, "Accept-Encoding": "gzip"}

    def fetch_api(self, prior: Mapping[str, Any], *, backfill: bool = False) -> SourceResult:
        """One LastPublishDate window: ``initial_lookback_days`` back on the
        first run or when ``backfill`` (a subscribed site has not stored the
        long history yet), else ``window_days`` (widened back to the last
        success after an outage)."""
        src = self._src(SOURCE_API)
        today = self.now.date()
        last = _parse_ts(prior.get("last_success_at"))
        window = int(src.get("window_days", 21))
        if backfill or last is None:
            backfill = True
            start = today - _dt.timedelta(days=int(src.get("initial_lookback_days", 730)))
        else:
            start = min(today - _dt.timedelta(days=window), last.date() - _dt.timedelta(days=2))
        params = {str(k): str(v) for k, v in (src.get("params") or {}).items()
                  if str(k) and v is not None}
        params.update({"format": "json", "LastPublishDateStart": start.isoformat(),
                       "LastPublishDateEnd": today.isoformat()})
        url = f"{src.get('url')}?{urllib.parse.urlencode(params)}"
        state: dict[str, Any] = {"window_start": start.isoformat(),
                                 "window_end": today.isoformat(), "backfill": backfill}
        try:
            resp = _get_with_retry(self.http_get, url, headers=self._headers("application/json"),
                                   timeout=float(src.get("timeout_s", 90)),
                                   retries=self.retries, base_delay=self.base_delay)
            if resp.status != 200:
                raise SourceError(f"SaferProducts API: HTTP {resp.status}")
            records = parse_api_records(resp.body)
        except (SourceError, _Retryable, urllib.error.URLError, OSError, ValueError) as e:
            return SourceResult(SOURCE_API, False, error=f"{type(e).__name__}: {e}"[:300],
                                state=state)
        state["records"] = len(records)
        return SourceResult(SOURCE_API, True, records=records, state=state,
                            detail={"window": [start.isoformat(), today.isoformat()]})

    def fetch_csv(self, name: str, prior: Mapping[str, Any],
                  parser: Callable[[Any], list[Recall]]) -> SourceResult:
        src = self._src(name)
        cache = (self.cache_dir / f"{name}.csv") if self.cache_dir else None
        headers = self._headers("text/csv,*/*;q=0.5")
        have_cache = cache is not None and cache.is_file()
        if have_cache:
            if prior.get("last_modified"):
                headers["If-Modified-Since"] = str(prior["last_modified"])
            if prior.get("etag"):
                headers["If-None-Match"] = str(prior["etag"])
        state: dict[str, Any] = {}
        not_modified = False
        try:
            resp = _get_with_retry(self.http_get, str(src.get("url")), headers=headers,
                                   timeout=float(src.get("timeout_s", 120)),
                                   retries=self.retries, base_delay=self.base_delay)
            if resp.status == 304 and have_cache and cache is not None:
                body = cache.read_bytes()
                not_modified = True
                state.update({k: prior.get(k) for k in ("etag", "last_modified")})
            elif resp.status == 200:
                body = resp.body
                state["etag"] = resp.headers.get("etag", "")
                state["last_modified"] = resp.headers.get("last-modified", "")
            else:
                raise SourceError(f"{name}: HTTP {resp.status}")
            records = parser(body)
            min_rows = int(src.get("min_rows", 0) or 0)
            if len(records) < min_rows:
                raise SourceError(f"{name}: only {len(records)} rows (< min_rows {min_rows}); "
                                  "treating the download as truncated")
            if resp.status == 200 and cache is not None:
                self._write_cache(cache, body)
        except (SourceError, _Retryable, urllib.error.URLError, OSError, ValueError,
                csv.Error) as e:
            return SourceResult(name, False, error=f"{type(e).__name__}: {e}"[:300])
        state["sha256"] = hashlib.sha256(body).hexdigest()
        state["records"] = len(records)
        state["not_modified"] = not_modified or state["sha256"] == prior.get("sha256")
        return SourceResult(name, True, records=records,
                            not_modified=bool(state["not_modified"]), state=state)

    @staticmethod
    def _write_cache(path: Path, body: bytes) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
            with os.fdopen(fd, "wb") as fh:
                fh.write(body)
            os.replace(tmp, path)
        except OSError as e:
            log.warning("recall_gate: CSV cache not written (%s); next run downloads in full", e)

    def fetch_all(self, prior: Mapping[str, Mapping[str, Any]], *,
                  backfill: bool = False) -> dict[str, SourceResult]:
        """Every enabled source; ``backfill`` asks the API for the long
        history (see ``fetch_api``)."""
        out: dict[str, SourceResult] = {}
        for name in self.enabled():
            p = prior.get(name) or {}
            try:
                if name == SOURCE_API:
                    out[name] = self.fetch_api(p, backfill=backfill)
                elif name == SOURCE_RECALL_CSV:
                    out[name] = self.fetch_csv(name, p, parse_recall_csv)
                else:
                    out[name] = self.fetch_csv(name, p, parse_warning_csv)
            except SourceError as e:  # a misconfigured source fails alone
                out[name] = SourceResult(name, False, error=str(e)[:300])
        return out


def merge_sources(results: Mapping[str, SourceResult],
                  remedy_cfg: Optional[Mapping[str, Any]] = None) -> dict[str, Recall]:
    """Successful source results -> merged records (precedence: warning CSV,
    recall CSV, then the API), remedy options normalised."""
    groups = [results[n].records for n in (SOURCE_WARNING_CSV, SOURCE_RECALL_CSV, SOURCE_API)
              if n in results and results[n].ok]
    merged = merge_records(*groups)
    rcfg = remedy_cfg or DEFAULTS["remedy_options"]
    return {k: with_remedy_options(v, rcfg) for k, v in merged.items()}


def next_source_state(prior: Optional[Mapping[str, Any]], result: Optional[SourceResult],
                      now: _dt.datetime) -> dict[str, Any]:
    """Carry-over state for one source: a failed fetch keeps the previous
    success time (sync age keeps growing) and the previous cache validators."""
    st = dict(prior or {})
    st["last_attempt_at"] = _iso(now)
    if result is None:
        return st
    if result.ok:
        st.update(result.state)
        st["last_success_at"] = _iso(now)
        st["last_error"] = ""
    else:
        st["last_error"] = result.error
    return st


def sync_age_hours(states: Mapping[str, Mapping[str, Any]], enabled: Sequence[str],
                   now: _dt.datetime) -> Optional[float]:
    """Hours since the stalest enabled source last succeeded; None when no
    source is enabled; 9999 when one has never succeeded."""
    if not enabled:
        return None
    worst = 0.0
    for name in enabled:
        ts = _parse_ts((states.get(name) or {}).get("last_success_at"))
        if ts is None:
            return 9999.0
        worst = max(worst, (now - ts).total_seconds() / 3600.0)
    return round(worst, 2)


# ---------------------------------------------------------------------------
# Database contract
# ---------------------------------------------------------------------------

SCHEMA_SQL = """\
-- recall-gate tables (framework/core/recall_gate.py, matcher v{matcher_version}).
-- Generated by: python3 -m framework.core.recall_gate schema
-- The gate writes these tables; the site renders ONLY from the views below.
-- The views are the single buy-button / pick / notice rule: never re-derive it
-- from recall_matches columns (a renderer and a /r/ redirect that disagree is
-- the failure this prevents). A match row may carry only model_id or only
-- product_id, so join a product to the views on
--   (v.product_id = p.id OR v.model_id = p.model_id).
-- Change the model_id / product_id types to your catalog's key types if they
-- differ.
CREATE TABLE IF NOT EXISTS {recalls} (
    id                text PRIMARY KEY,               -- cpsc-recall-26010 | cpsc-warning-25454
    source            text NOT NULL,                  -- cpsc-recall | cpsc-warning
    kind              text NOT NULL CHECK (kind IN ('recall', 'warning')),
    number            text NOT NULL,
    url               text,
    url_derived       boolean NOT NULL DEFAULT false,
    title             text NOT NULL,
    published_at      date,
    last_published_at date,
    remedy            text,
    remedy_options    text[] NOT NULL DEFAULT '{{}}',
    hazard            text,
    units             text,
    units_count       integer,
    product_names     text[] NOT NULL DEFAULT '{{}}',
    sold_at           text,
    tier              text NOT NULL CHECK (tier IN ('stop-use', 'repair-hold')),
    hold_until        date,                           -- repair-hold end (recall-level)
    categories        text[] NOT NULL DEFAULT '{{}}', -- this site's categories it falls in
    related_ids       text[] NOT NULL DEFAULT '{{}}', -- warning <-> recall
    sources           text[] NOT NULL DEFAULT '{{}}',
    record            jsonb NOT NULL,                 -- full normalised record
    content_sha       text NOT NULL,
    first_seen_at     timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS {recalls_short}_published_idx ON {recalls} (published_at DESC);

CREATE TABLE IF NOT EXISTS {matches} (
    id                bigserial PRIMARY KEY,
    match_key         text NOT NULL UNIQUE,           -- <recall id>|m:<model_id>|p:<product_id>
    recall_id         text NOT NULL REFERENCES {recalls} (id) ON DELETE CASCADE,
    model_id          bigint,
    product_id        bigint,
    item_label        text,
    -- {{url, slug, asin}} of the catalog row when last matched: the buy-button
    -- probe still checks a blocked product's page after it leaves catalog_sql.
    item_locator      jsonb,
    match_kind        text NOT NULL CHECK (match_kind IN ('exact', 'sibling', 'ambiguous')),
    tier              text NOT NULL CHECK (tier IN ('stop-use', 'repair-hold', 'review')),
    proposed_tier     text NOT NULL CHECK (proposed_tier IN ('stop-use', 'repair-hold')),
    status            text NOT NULL CHECK (status IN ('active', 'manual-review', 'cleared')),
    hold_until        date,
    reason            text NOT NULL,
    evidence          jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    matcher_version   text NOT NULL,
    reviewer          text,                           -- set => the gate never overwrites the decision
    reviewed_at       timestamptz,
    review_note       text,
    first_matched_at  timestamptz NOT NULL DEFAULT now(),
    stale_since       timestamptz,                    -- the gate no longer finds this match
    -- The gate wants a human: it would lift this block (it never does that by
    -- itself), it no longer finds the match for a live catalog row, a match a
    -- human cleared now has stronger evidence, or it blocked a product over a
    -- review decision (it always applies a stricter block by itself).
    -- gate_proposal holds what the gate found, why, and any earlier human
    -- decision it went past.
    review_requested_at timestamptz,
    gate_proposal     jsonb,
    notified_at       timestamptz,
    notified_key      text,                           -- state last emailed (see notification_key)
    updated_at        timestamptz NOT NULL DEFAULT now(),
    CHECK (model_id IS NOT NULL OR product_id IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS {matches_short}_product_idx ON {matches} (product_id) WHERE status <> 'cleared';
CREATE INDEX IF NOT EXISTS {matches_short}_model_idx ON {matches} (model_id) WHERE status <> 'cleared';
CREATE INDEX IF NOT EXISTS {matches_short}_queue_idx ON {matches} (first_matched_at)
    WHERE status = 'manual-review' OR review_requested_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS {sync_log} (
    id                 bigserial PRIMARY KEY,
    synced_at          timestamptz NOT NULL DEFAULT now(),
    matcher_version    text NOT NULL,
    dry_run            boolean NOT NULL DEFAULT false,
    sources            jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    recalls_considered integer NOT NULL DEFAULT 0,
    recalls_relevant   integer NOT NULL DEFAULT 0,
    recalls_written    integer NOT NULL DEFAULT 0,
    matches_total      integer NOT NULL DEFAULT 0,
    matches_new        integer NOT NULL DEFAULT 0,
    matches_updated    integer NOT NULL DEFAULT 0,
    matches_stale      integer NOT NULL DEFAULT 0,
    blocking           integer NOT NULL DEFAULT 0,
    manual_review      integer NOT NULL DEFAULT 0
);

-- No buy button for these rows (stop-use, or repair-hold before hold_until).
CREATE OR REPLACE VIEW {view_blocks} AS
SELECT m.product_id, m.model_id, m.recall_id, m.tier, m.hold_until, m.match_key
  FROM {matches} m
 WHERE m.status = 'active'
   AND (m.tier = 'stop-use'
        OR (m.tier = 'repair-hold' AND (m.hold_until IS NULL OR m.hold_until > CURRENT_DATE)));

-- Never pick, ladder or substitute these rows: every match that is not
-- cleared (design 0008_views: v_pick_eligible requires "no active or
-- manual-review recall match"), including a confirmed recall whose repair
-- hold has ended and keeps its buy button.
CREATE OR REPLACE VIEW {view_picks} AS
SELECT m.product_id, m.model_id, m.recall_id, m.status, m.tier, m.hold_until, m.match_key
  FROM {matches} m
 WHERE m.status IN ('active', 'manual-review');

-- Recall notice / banner data for product pages (every confirmed match).
CREATE OR REPLACE VIEW {view_notices} AS
SELECT m.product_id, m.model_id, m.match_key, m.tier, m.hold_until,
       r.id AS recall_id, r.kind, r.number, r.title, r.url, r.published_at, r.hazard, r.remedy
  FROM {matches} m JOIN {recalls} r ON r.id = m.recall_id
 WHERE m.status = 'active';
"""


def schema_sql(tables: Optional[Mapping[str, str]] = None) -> str:
    """The reference DDL for a site database (the site's migration owns it;
    the gate only reads and writes)."""
    t = {k: _ident(v, k) for k, v in _merge(DEFAULT_TABLES, tables).items()}
    schema = t["matches"].rsplit(".", 1)[0] + "." if "." in t["matches"] else ""
    return SCHEMA_SQL.format(
        matcher_version=MATCHER_VERSION, recalls=t["recalls"], matches=t["matches"],
        sync_log=t["sync_log"], recalls_short=t["recalls"].split(".")[-1],
        matches_short=t["matches"].split(".")[-1],
        view_blocks=schema + "v_recall_buy_blocks",
        view_picks=schema + "v_recall_pick_exclusions",
        view_notices=schema + "v_recall_notices")


def _json_adapt(value: Any) -> Any:
    from psycopg2.extras import Json
    return Json(value, dumps=_stable_json)


def _rows(cur: Any) -> list[dict[str, Any]]:
    cols = [c[0] for c in (cur.description or [])]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def advisory_lock_id(site: str) -> int:
    return int.from_bytes(hashlib.sha256(f"recall-gate:{site}".encode()).digest()[:8],
                          "big", signed=True)


def load_catalog(conn: Any, settings: SiteSettings) -> tuple[list[CatalogItem], list[str]]:
    with conn.cursor() as cur:
        cur.execute(settings.catalog_sql, dict(settings.catalog_params) or None)
        rows = _rows(cur)
    return catalog_from_rows(rows)


def load_known_recalls(conn: Any, settings: SiteSettings) -> dict[str, Recall]:
    """Records this site already stores (so a source outage never shrinks
    the set the catalog is re-checked against)."""
    out: dict[str, Recall] = {}
    with conn.cursor() as cur:
        cur.execute(f"SELECT id, record FROM {settings.table('recalls')}")
        for rid, record in cur.fetchall():
            doc = record if isinstance(record, Mapping) else json.loads(record or "{}")
            try:
                rec = Recall.from_dict(doc)
            except (TypeError, ValueError):
                continue
            if rec.id == rid:
                out[rid] = rec
    return out


_MATCH_COLS = ("match_key", "recall_id", "model_id", "product_id", "item_label", "item_locator",
               "match_kind", "tier", "proposed_tier", "status", "hold_until", "reason", "evidence",
               "matcher_version", "reviewer", "reviewed_at", "review_note", "first_matched_at",
               "stale_since", "review_requested_at", "gate_proposal", "notified_at",
               "notified_key")


def load_existing_matches(conn: Any, settings: SiteSettings) -> dict[str, dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(_MATCH_COLS)} FROM {settings.table('matches')}")
        return {r["match_key"]: r for r in _rows(cur)}


@dataclass
class SiteSyncResult:
    site: str
    ok: bool = True
    error: str = ""
    dry_run: bool = False
    catalog_items: int = 0
    catalog_problems: list[str] = field(default_factory=list)
    recalls_considered: int = 0
    recalls_relevant: int = 0
    recalls_inserted: int = 0
    recalls_updated: int = 0
    matches: list[MatchResult] = field(default_factory=list)
    matches_new: int = 0
    matches_updated: int = 0
    matches_stale: int = 0
    blocking: int = 0
    # Rows in the manual queue (manual-review, or a review the gate requested).
    manual_review: int = 0
    review_requested: int = 0
    new_keys: list[str] = field(default_factory=list)
    # Every blocked product (one per catalog row): the catalog row itself, or,
    # for one that left catalog_sql, a stand-in built from the match row (its
    # stored page locator and label) so the buy-button probe still checks it.
    blocked_items: list[CatalogItem] = field(default_factory=list)
    # Keys of the blocked products catalog_sql no longer returns.
    blocked_unlisted: list[str] = field(default_factory=list)
    # The site's manual queue as this transaction leaves it (a dry run sees
    # what it would leave, not the rolled-back database).
    queue: list[dict[str, Any]] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        out = {k: getattr(self, k) for k in (
            "site", "ok", "error", "dry_run", "catalog_items", "recalls_considered",
            "recalls_relevant", "recalls_inserted", "recalls_updated", "matches_new",
            "matches_updated", "matches_stale", "blocking", "manual_review",
            "review_requested")}
        out["matches_total"] = len(self.matches)
        out["blocked_unlisted"] = len(self.blocked_unlisted)
        return out


_RECALL_COLS = ("id", "source", "kind", "number", "url", "url_derived", "title",
                "published_at", "last_published_at", "remedy", "remedy_options", "hazard",
                "units", "units_count", "product_names", "sold_at", "tier", "hold_until",
                "categories", "related_ids", "sources", "record", "content_sha")


def _upsert_recall(cur: Any, table: str, rec: Recall, decision: TierDecision,
                   categories: Sequence[str], now: _dt.datetime) -> Optional[bool]:
    """True inserted, False updated, None unchanged. ``hold_until`` here is
    the recall-level date (recall-date anchored); a product's hold is
    recall_matches.hold_until."""
    values = {
        "id": rec.id, "source": rec.source_label, "kind": rec.kind,
        "number": rec.number or rec.number_key, "url": rec.url or None,
        "url_derived": rec.url_derived, "title": rec.title or rec.number or rec.id,
        "published_at": rec.published_at, "last_published_at": rec.last_published_at,
        "remedy": rec.remedy or None, "remedy_options": list(rec.remedy_options),
        "hazard": rec.hazard or None, "units": rec.units or None,
        "units_count": rec.units_count, "product_names": list(rec.product_names),
        "sold_at": rec.sold_at or None, "tier": decision.tier,
        "hold_until": hold_until_for(decision, rec),
        "categories": sorted(categories), "related_ids": list(rec.related_ids),
        "sources": list(rec.sources), "record": _json_adapt(rec.to_dict()),
        "content_sha": rec.content_sha(), "now": now,
    }
    cols = ", ".join(_RECALL_COLS)
    vals = ", ".join(f"%({c})s" for c in _RECALL_COLS)
    sets = ", ".join(f"{c} = EXCLUDED.{c}" for c in _RECALL_COLS if c != "id")
    cur.execute(f"""
        INSERT INTO {table} AS r ({cols}, first_seen_at, updated_at)
        VALUES ({vals}, %(now)s, %(now)s)
        ON CONFLICT (id) DO UPDATE SET {sets}, updated_at = EXCLUDED.updated_at
        WHERE r.content_sha IS DISTINCT FROM EXCLUDED.content_sha
           OR r.tier IS DISTINCT FROM EXCLUDED.tier
           OR r.hold_until IS DISTINCT FROM EXCLUDED.hold_until
           OR r.categories IS DISTINCT FROM EXCLUDED.categories
           OR r.sources IS DISTINCT FROM EXCLUDED.sources
           OR r.url IS DISTINCT FROM EXCLUDED.url
        RETURNING (xmax = 0) AS inserted""", values)
    row = cur.fetchone()
    return None if row is None else bool(row[0])


def _json_norm(value: Any) -> Any:
    """A JSON value in the shape the database hands back (for comparing)."""
    if value is None:
        return None
    if isinstance(value, (str, bytes, bytearray)):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return json.loads(_stable_json(value))


# gate_proposal.why: why the gate asked for a human (review_requested_at).
WHY_WOULD_LIFT = "gate-would-lift"
WHY_NO_LONGER_MATCHES = "gate-no-longer-matches"
WHY_STRONGER_AFTER_CLEAR = "stronger-evidence-after-clear"
WHY_ESCALATED = "gate-escalated-over-review"
# Requests that carry an earlier HUMAN decision the gate went past: they stay
# until a human decides (review_match), whatever the gate finds later.
_HUMAN_REQUESTS = frozenset({WHY_STRONGER_AFTER_CLEAR, WHY_ESCALATED})
_TIER_RANK = {TIER_REVIEW: 0, TIER_REPAIR_HOLD: 1, TIER_STOP_USE: 2}
_JSON_MATCH_COLS = frozenset({"evidence", "gate_proposal", "item_locator"})


def _proposal(m: MatchResult, why: str, **extra: Any) -> dict[str, Any]:
    return {"why": why, "match_kind": m.match_kind, "tier": m.tier, "status": m.status,
            "hold_until": m.hold_until, "rule": m.rule, "reason": m.reason[:300],
            "matcher_version": MATCHER_VERSION, **extra}


def item_locator(item: CatalogItem) -> Optional[dict[str, str]]:
    """What ``recall_matches.item_locator`` keeps of a catalog row: its url,
    slug and asin (whichever it has), so the buy-button probe can still find
    a blocked product's page after the product leaves catalog_sql."""
    loc = {k: str(v) for k, v in (("url", item.url), ("slug", item.slug), ("asin", item.asin))
           if v}
    return loc or None


def _block_level(tier: Any, status: Any, hold_until: Any, today: _dt.date
                 ) -> tuple[int, _dt.date]:
    """How hard a state blocks the buy button as of ``today``; larger is
    stricter: (0, min) not at all, (1, end) a repair hold until ``end``
    (date.max when open-ended), (2, max) stop-use."""
    if not is_blocking(str(tier or ""), str(status or ""), hold_until, today):
        return (0, _dt.date.min)
    if tier == TIER_STOP_USE:
        return (2, _dt.date.max)
    return (1, _as_date(hold_until) or _dt.date.max)


def _recall_escalation(row: Mapping[str, Any], recall_tier: Optional[str]
                       ) -> Optional[dict[str, Any]]:
    """{"tier", "hold_until"} when the recall's own tier rose since this
    ACTIVE row was last written (``proposed_tier`` holds the tier it had
    then; a repair recall re-announced as a refund) and now exceeds the
    row's tier. An active row says the product IS covered by the recall,
    whoever decided that, so the recall's remedy reaches it. It fires once
    (proposed_tier is refreshed with it): a human who then confirms a lower
    tier on purpose is not overruled again."""
    was = row.get("proposed_tier")
    rank = _TIER_RANK.get(str(recall_tier or ""), 0)
    if (row.get("status") != STATUS_ACTIVE or was not in BLOCKING_TIERS
            or rank <= _TIER_RANK[str(was)] or rank <= _TIER_RANK.get(str(row.get("tier")), 0)):
        return None
    hold = None if recall_tier == TIER_STOP_USE else _as_date(row.get("hold_until"))
    return {"tier": recall_tier, "hold_until": hold}


def _human_decision(row: Mapping[str, Any]) -> dict[str, Any]:
    """The review decision on a row, for gate_proposal when the gate goes
    past it."""
    hold = _as_date(row.get("hold_until"))
    return {"reviewer": row.get("reviewer"), "reviewed_at": _iso(_parse_ts(row.get("reviewed_at"))),
            "match_kind": row.get("match_kind"), "status": row.get("status"),
            "tier": row.get("tier"), "hold_until": hold.isoformat() if hold else None}


def _diff_match(old: Mapping[str, Any], want: Mapping[str, Any]) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    for k, v in want.items():
        cur = old.get(k)
        if k in _JSON_MATCH_COLS:
            if _json_norm(cur) != _json_norm(v):
                changes[k] = v
        elif k == "hold_until":
            if _as_date(cur) != _as_date(v):
                changes[k] = _as_date(v)
        elif cur != v:
            changes[k] = v
    return changes


def plan_match_update(old: Mapping[str, Any], new: MatchResult, *, today: _dt.date,
                      now: _dt.datetime) -> dict[str, Any]:
    """The columns to change on an existing recall_matches row ({} = none).

    One-directional: the gate never LIFTS a block by itself, and it always
    APPLIES a stricter one, over a pending request or a review decision too
    (the human is then asked, and emailed, about what it did).

    0. A recall-level escalation (``_recall_escalation``: the recall's tier
       rose, say a repair recall re-announced as a refund) reaches every
       ACTIVE row of it, whoever decided that row.
    1. A human decision (``reviewer`` set) is kept; the evidence refreshes.
       Exceptions:
       a. a CLEARED row whose gate evidence is now stronger than the kind the
          reviewer saw (ambiguous < sibling < exact): when the new result
          blocks, it is applied (reviewer NULL, the clear kept in
          gate_proposal, review requested); otherwise the row goes back to
          the manual queue (manual-review, review requested, buy button
          unchanged);
       b. a CONFIRMED (active) row whose recall escalated (0) takes the
          stricter tier and requests a review; the reviewer stays (they
          confirmed the match itself).
    2. A row already waiting for a human (``review_requested_at``) keeps its
       decision (plus 0) unless the gate's result blocks harder, which is
       applied. A gate-only request (would lift, no longer matches) is
       withdrawn once the stored decision and the gate's result agree; a
       request that carries a human decision (``_HUMAN_REQUESTS``) stays
       until a human decides. The proposal refreshes.
    3. The gate never demotes an ``active`` row by itself (to manual-review,
       or from blocking to not blocking as of ``today``): it keeps the
       decision (plus 0) and requests a review, with what it found in
       ``gate_proposal``.
    4. Anything else is applied."""
    want: dict[str, Any] = {
        "item_label": new.item.display[:300], "item_locator": item_locator(new.item),
        "proposed_tier": new.proposed_tier, "reason": new.reason,
        "evidence": dict(new.evidence), "matcher_version": MATCHER_VERSION, "stale_since": None,
    }
    new_state = {"match_kind": new.match_kind, "tier": new.tier, "status": new.status,
                 "hold_until": _as_date(new.hold_until)}
    old_state = {"match_kind": old.get("match_kind"), "tier": old.get("tier"),
                 "status": old.get("status"), "hold_until": _as_date(old.get("hold_until"))}
    prev = _json_norm(old.get("gate_proposal"))
    prev = prev if isinstance(prev, dict) else {}
    up = _recall_escalation(old, new.proposed_tier)
    stored = {**old_state, **(up or {})}
    if old.get("reviewer"):
        stronger = (_KIND_RANK.get(new.match_kind, 0)
                    > _KIND_RANK.get(str(old_state["match_kind"]), 0))
        if old_state["status"] == STATUS_CLEARED and stronger and new.blocking(today):
            want.update(new_state)
            want.update({"reviewer": None, "reviewed_at": None, "review_requested_at": now,
                         "gate_proposal": _proposal(new, WHY_ESCALATED,
                                                    human_decision=_human_decision(old),
                                                    cleared_by=old.get("reviewer"),
                                                    cleared_kind=old_state["match_kind"])})
        elif old_state["status"] == STATUS_CLEARED and stronger:
            want.update({"match_kind": new.match_kind, "tier": TIER_REVIEW,
                         "status": STATUS_REVIEW, "hold_until": None, "reviewer": None,
                         "reviewed_at": None, "review_requested_at": now,
                         "gate_proposal": _proposal(new, WHY_STRONGER_AFTER_CLEAR,
                                                    cleared_by=old.get("reviewer"),
                                                    cleared_kind=old_state["match_kind"])})
        elif up:
            want.update(up)
            want.update({"review_requested_at": old.get("review_requested_at") or now,
                         "gate_proposal": _proposal(new, WHY_ESCALATED,
                                                    human_decision=_human_decision(old),
                                                    recall_tier_was=old.get("proposed_tier"),
                                                    recall_tier=new.proposed_tier)})
    elif old.get("review_requested_at") is not None:
        human = prev.get("why") in _HUMAN_REQUESTS
        if (_block_level(new.tier, new.status, new.hold_until, today)
                > _block_level(stored["tier"], stored["status"], stored["hold_until"], today)):
            want.update(new_state)
            if human:
                want["gate_proposal"] = {**prev, **_proposal(new, WHY_ESCALATED)}
            else:
                want.update({"review_requested_at": None, "gate_proposal": None})
        else:
            want.update(stored)
            if stored == new_state and not human:
                want.update({"review_requested_at": None, "gate_proposal": None})
            else:
                want["gate_proposal"] = {**prev, **_proposal(
                    new, str(prev.get("why") or WHY_WOULD_LIFT))}
    else:
        demote = stored["status"] == STATUS_ACTIVE and (
            new.status != STATUS_ACTIVE
            or (is_blocking(str(stored["tier"]), STATUS_ACTIVE, stored["hold_until"], today)
                and not new.blocking(today)))
        if demote:
            want.update(stored)
            want.update({"review_requested_at": now,
                         "gate_proposal": _proposal(new, WHY_WOULD_LIFT)})
        else:
            want.update(new_state)
    return _diff_match(old, want)


def plan_stale_update(row: Mapping[str, Any], *, recall_tier: Optional[str], listed: bool,
                      now: _dt.datetime) -> dict[str, Any]:
    """The columns to change on a row whose match the gate no longer finds
    (besides ``stale_since``, which sync_site sets). Nothing is lifted:

    * the recall's current tier refreshes ``proposed_tier``, and a
      recall-level escalation (``_recall_escalation``) still reaches an
      ACTIVE row; over a review decision, for a product still listed, the
      gate also asks the human again (``gate-escalated-over-review``);
    * an unreviewed active row of a product the catalog still lists asks a
      human (``gate-no-longer-matches``); a product that left the catalog
      needs no one."""
    want: dict[str, Any] = {}
    if recall_tier in BLOCKING_TIERS:
        want["proposed_tier"] = recall_tier
    up = _recall_escalation(row, recall_tier)
    if up:
        want.update(up)
    requested = row.get("review_requested_at") is not None
    if up and row.get("reviewer") and listed:
        hold = _as_date(up["hold_until"])
        want.update({"review_requested_at": row.get("review_requested_at") or now,
                     "gate_proposal": {
                         "why": WHY_ESCALATED, "match_kind": row.get("match_kind"),
                         "tier": up["tier"], "status": STATUS_ACTIVE,
                         "hold_until": hold.isoformat() if hold else None,
                         "human_decision": _human_decision(row),
                         "recall_tier_was": row.get("proposed_tier"), "recall_tier": recall_tier,
                         "reason": "the recall's own tier rose; the gate no longer finds this "
                                   "match itself, the review decision says it is covered",
                         "matcher_version": MATCHER_VERSION}})
    elif (row.get("reviewer") is None and not requested and row.get("status") == STATUS_ACTIVE
          and listed):
        want.update({"review_requested_at": now,
                     "gate_proposal": {"why": WHY_NO_LONGER_MATCHES,
                                       "matcher_version": MATCHER_VERSION,
                                       "reason": "the gate no longer finds this match "
                                                 "for a catalog row it still lists"}})
    return _diff_match(row, want)


_UPDATABLE_MATCH_COLS = frozenset({
    "item_label", "item_locator", "proposed_tier", "reason", "evidence", "matcher_version",
    "stale_since", "match_kind", "tier", "status", "hold_until", "reviewer", "reviewed_at",
    "review_requested_at", "gate_proposal"})


def _insert_match(cur: Any, table: str, m: MatchResult, now: _dt.datetime) -> bool:
    loc = item_locator(m.item)
    cur.execute(f"""
        INSERT INTO {table} (match_key, recall_id, model_id, product_id, item_label, item_locator,
                             match_kind, tier, proposed_tier, status, hold_until, reason,
                             evidence, matcher_version, first_matched_at, updated_at)
        VALUES (%(match_key)s, %(recall_id)s, %(model_id)s, %(product_id)s, %(item_label)s,
                %(item_locator)s, %(match_kind)s, %(tier)s, %(proposed_tier)s, %(status)s,
                %(hold_until)s, %(reason)s, %(evidence)s, %(matcher_version)s, %(now)s, %(now)s)
        ON CONFLICT (match_key) DO NOTHING
        RETURNING id""", {
        "match_key": m.key, "recall_id": m.recall_id, "model_id": m.item.model_id,
        "product_id": m.item.product_id, "item_label": m.item.display[:300],
        "item_locator": _json_adapt(loc) if loc else None,
        "match_kind": m.match_kind, "tier": m.tier, "proposed_tier": m.proposed_tier,
        "status": m.status, "hold_until": m.hold_until, "reason": m.reason,
        "evidence": _json_adapt(dict(m.evidence)), "matcher_version": MATCHER_VERSION,
        "now": now})
    return cur.fetchone() is not None


def _update_match(cur: Any, table: str, key: str, changes: Mapping[str, Any],
                  now: _dt.datetime) -> None:
    sets: list[str] = []
    params: dict[str, Any] = {"k": key, "now": now}
    for i, (col, val) in enumerate(sorted(changes.items())):
        if col not in _UPDATABLE_MATCH_COLS:
            raise ValueError(f"not an updatable recall_matches column: {col}")
        sets.append(f"{col} = %(v{i})s")
        params[f"v{i}"] = (_json_adapt(val) if col in _JSON_MATCH_COLS
                           and val is not None else val)
    cur.execute(f"UPDATE {table} SET {', '.join(sets)}, updated_at = %(now)s "
                "WHERE match_key = %(k)s", params)


def build_site_views(known: Mapping[str, Recall], views: Mapping[str, RecallView],
                     remedy_cfg: Mapping[str, Any]) -> dict[str, RecallView]:
    """Fetched views plus every record the site already stores. A stored
    record that the fetch also returned is merged (fetched data on top), so
    a URL or caption learnt earlier survives a CSV-only day."""
    out: dict[str, RecallView] = {}
    for rid, rec in known.items():
        fresh = views.get(rid)
        if fresh is None:
            out[rid] = RecallView(with_remedy_options(rec, remedy_cfg))
            continue
        merged = with_remedy_options(merge_recall(rec, fresh.recall), remedy_cfg)
        out[rid] = fresh if merged == fresh.recall else RecallView(merged)
    for rid, view in views.items():
        out.setdefault(rid, view)
    return out


_QUEUE_SQL = """
    SELECT m.match_key, m.recall_id, m.model_id, m.product_id, m.item_label,
           m.match_kind, m.tier, m.status, m.proposed_tier, m.reason, m.first_matched_at,
           m.stale_since, m.review_requested_at, m.gate_proposal,
           r.title, r.number, r.kind, r.url
      FROM {t_match} m JOIN {t_rec} r ON r.id = m.recall_id
     WHERE m.status = 'manual-review' OR m.review_requested_at IS NOT NULL
     ORDER BY COALESCE(m.review_requested_at, m.first_matched_at), m.match_key"""


def _queue_rows(cur: Any, settings: SiteSettings, now: _dt.datetime) -> list[dict[str, Any]]:
    cur.execute(_QUEUE_SQL.format(t_match=settings.table("matches"),
                                  t_rec=settings.table("recalls")))
    out = []
    for r in _rows(cur):
        since = _parse_ts(r.get("review_requested_at")) or _parse_ts(r.get("first_matched_at"))
        doc = {k: (_iso(v) if isinstance(v, _dt.datetime)
                   else v.isoformat() if isinstance(v, _dt.date) else v) for k, v in r.items()}
        doc["site"] = settings.site
        doc["age_h"] = round((now - since).total_seconds() / 3600.0, 1) if since else None
        out.append(doc)
    return out


def sync_site(conn: Any, settings: SiteSettings, views: Mapping[str, RecallView], *,
              now: Optional[_dt.datetime] = None, dry_run: bool = False,
              source_summary: Optional[Mapping[str, Any]] = None) -> SiteSyncResult:
    """Match ``views`` (plus every recall the site already stores) against
    the site's catalog and write the result in ONE transaction under the
    site's advisory lock. Idempotent: the same inputs write nothing the
    second time except the sync-log row. ``dry_run`` rolls back (the result
    still reports what the run would have left, queue included)."""
    now = now or _utcnow()
    today = now.date()
    res = SiteSyncResult(site=settings.site, dry_run=dry_run)
    t_rec, t_match, t_log = (settings.table("recalls"), settings.table("matches"),
                             settings.table("sync_log"))
    if getattr(conn, "autocommit", False):
        conn.autocommit = False  # one transaction for the whole site write
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_xact_lock(%s)", (advisory_lock_id(settings.site),))
            if not cur.fetchone()[0]:
                raise SiteLockedError(f"site {settings.site}: another recall-gate run or a "
                                      "review holds the lock")
            cur.execute(f"SET LOCAL statement_timeout = {int(settings.statement_timeout_ms)}")
        items, problems = load_catalog(conn, settings)
        res.catalog_items, res.catalog_problems = len(items), problems
        catalog_keys = {it.key for it in items}
        known = load_known_recalls(conn, settings)
        existing = load_existing_matches(conn, settings)
        all_views = build_site_views(known, views, settings.remedy)
        res.recalls_considered = len(all_views)
        matcher = Matcher(settings, items)
        first_matched = {k: _parse_ts(v.get("first_matched_at")) for k, v in existing.items()}
        to_store: dict[str, tuple[Recall, TierDecision, tuple[str, ...]]] = {}
        decisions: dict[str, TierDecision] = {}
        matches: list[MatchResult] = []
        for rid in sorted(all_views):
            view = all_views[rid]
            decision = decisions[rid] = decide_tier(view.recall, settings.tier_rules)
            cats, found = matcher.match(view, decision=decision, first_matched=first_matched,
                                        now=now)
            if cats:
                res.recalls_relevant += 1
            if cats or found or rid in known:
                to_store[rid] = (view.recall, decision, cats)
            matches.extend(found)
        res.matches = matches
        with conn.cursor() as cur:
            for rid in sorted(to_store):
                rec, decision, cats = to_store[rid]
                r = _upsert_recall(cur, t_rec, rec, decision, cats, now)
                if r is True:
                    res.recalls_inserted += 1
                elif r is False:
                    res.recalls_updated += 1
            current: set[str] = set()
            for m in matches:
                current.add(m.key)
                old = existing.get(m.key)
                if old is None:
                    if _insert_match(cur, t_match, m, now):
                        res.matches_new += 1
                        res.new_keys.append(m.key)
                    continue
                changes = plan_match_update(old, m, today=today, now=now)
                if changes:
                    _update_match(cur, t_match, m.key, changes, now)
                    res.matches_updated += 1
            stale = sorted(k for k, row in existing.items()
                           if k not in current and row.get("status") != STATUS_CLEARED)
            if stale:
                cur.execute(f"""UPDATE {t_match} SET stale_since = %(now)s, updated_at = %(now)s
                                 WHERE match_key = ANY(%(keys)s) AND stale_since IS NULL""",
                            {"now": now, "keys": stale})
                # The decision stays (a block stays a block, and a recall
                # that escalated still reaches it); a human is asked when the
                # catalog still lists the product (plan_stale_update).
                for k in stale:
                    row = existing[k]
                    dec = decisions.get(str(row.get("recall_id")))
                    changes = plan_stale_update(
                        row, recall_tier=dec.tier if dec else None, now=now,
                        listed=item_key(row.get("model_id"), row.get("product_id"))
                        in catalog_keys)
                    if changes:
                        _update_match(cur, t_match, k, changes, now)
            res.matches_stale = len(stale)
            for stmt in settings.post_sync_sql:
                cur.execute(stmt)
            cur.execute(f"""SELECT match_key, model_id, product_id, tier, status, hold_until,
                                   review_requested_at, item_label, item_locator
                              FROM {t_match} WHERE status <> 'cleared'
                             ORDER BY updated_at DESC, match_key""")
            by_key = {it.key: it for it in items}
            blocked: dict[str, CatalogItem] = {}
            for row in _rows(cur):
                if row.get("review_requested_at") is not None:
                    res.review_requested += 1
                if not is_blocking(row["tier"], row["status"], row["hold_until"], today):
                    continue
                ik = item_key(row["model_id"], row["product_id"])
                if ik in blocked:
                    continue
                if ik in by_key:
                    blocked[ik] = by_key[ik]
                    continue
                # Left catalog_sql: a stand-in from the newest match row's
                # stored page locator, so its page is still checked.
                loc = _json_norm(row.get("item_locator"))
                loc = loc if isinstance(loc, dict) else {}
                blocked[ik] = CatalogItem(
                    product_id=row["product_id"], model_id=row["model_id"],
                    model_name=clean_text(row.get("item_label")),
                    url=clean_text(loc.get("url")), slug=clean_text(loc.get("slug")),
                    asin=clean_text(loc.get("asin")).upper())
                res.blocked_unlisted.append(ik)
            res.blocked_items = [blocked[k] for k in sorted(blocked)]
            res.blocked_unlisted.sort()
            res.blocking = len(blocked)
            res.queue = _queue_rows(cur, settings, now)
            res.manual_review = len(res.queue)
            cur.execute(f"""INSERT INTO {t_log} (synced_at, matcher_version, dry_run, sources,
                                recalls_considered, recalls_relevant, recalls_written,
                                matches_total, matches_new, matches_updated, matches_stale,
                                blocking, manual_review)
                            VALUES (%(now)s, %(mv)s, %(dry)s, %(sources)s, %(rc)s, %(rr)s,
                                    %(rw)s, %(mt)s, %(mn)s, %(mu)s, %(ms)s, %(bl)s, %(mr)s)""",
                        {"now": now, "mv": MATCHER_VERSION, "dry": dry_run,
                         "sources": _json_adapt(dict(source_summary or {})),
                         "rc": res.recalls_considered, "rr": res.recalls_relevant,
                         "rw": res.recalls_inserted + res.recalls_updated,
                         "mt": len(matches), "mn": res.matches_new,
                         "mu": res.matches_updated, "ms": res.matches_stale,
                         "bl": res.blocking, "mr": res.manual_review})
        if dry_run:
            conn.rollback()
        else:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    return res


# What a match row was last emailed as: kind/tier/status/hold_until/review.
NOTIFICATION_KEY_SQL = ("m.match_kind || '/' || m.tier || '/' || m.status || '/' || "
                        "COALESCE(to_char(m.hold_until, 'YYYY-MM-DD'), '') || '/' || "
                        "CASE WHEN m.review_requested_at IS NULL THEN '' ELSE 'review' END")


def notification_key(row: Mapping[str, Any]) -> str:
    """The Python twin of NOTIFICATION_KEY_SQL (they must agree)."""
    hold = _as_date(row.get("hold_until"))
    return "/".join([str(row.get("match_kind") or ""), str(row.get("tier") or ""),
                     str(row.get("status") or ""), hold.isoformat() if hold else "",
                     "review" if row.get("review_requested_at") else ""])


def _parse_notification_key(key: Any) -> Optional[dict[str, Any]]:
    if not key:
        return None
    parts = str(key).split("/")
    parts += [""] * (5 - len(parts))
    return {"match_kind": parts[0], "tier": parts[1], "status": parts[2],
            "hold_until": parts[3] or None, "review": parts[4] == "review"}


def pending_notifications(conn: Any, settings: SiteSettings) -> list[dict[str, Any]]:
    """Live matches whose current state was never emailed: unreviewed rows,
    and any row waiting for a review the gate requested (a reviewed row the
    gate escalated over the review included)."""
    t_rec, t_match = settings.table("recalls"), settings.table("matches")
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT m.match_key, m.recall_id, m.model_id, m.product_id, m.item_label,
                   m.match_kind, m.tier, m.proposed_tier, m.status, m.hold_until, m.reason,
                   m.evidence, m.first_matched_at, m.notified_key, m.review_requested_at,
                   m.gate_proposal, m.stale_since,
                   r.title, r.url, r.number, r.kind, r.published_at, r.remedy, r.hazard,
                   r.units
              FROM {t_match} m JOIN {t_rec} r ON r.id = m.recall_id
             WHERE (m.reviewer IS NULL OR m.review_requested_at IS NOT NULL)
               AND m.status <> 'cleared'
               AND (m.stale_since IS NULL OR m.review_requested_at IS NOT NULL)
               AND m.notified_key IS DISTINCT FROM ({NOTIFICATION_KEY_SQL})
             ORDER BY COALESCE(m.review_requested_at, m.first_matched_at), m.match_key""")
        rows = _rows(cur)
    conn.commit()
    return rows


def needs_email(row: Mapping[str, Any], today: Optional[_dt.date] = None) -> bool:
    """Email when the match is new, when the buy button flips either way (as
    of ``today``), when it becomes a confirmed (active) match, or when the
    gate newly asks for a human. Other state changes only update the
    bookkeeping."""
    prev = _parse_notification_key(row.get("notified_key"))
    if prev is None:
        return True
    today = today or _utcnow().date()
    status = str(row.get("status") or "")
    now_blocking = is_blocking(str(row.get("tier") or ""), status, row.get("hold_until"), today)
    was_blocking = is_blocking(prev["tier"], prev["status"], prev["hold_until"], today)
    if now_blocking != was_blocking:
        return True
    if status == STATUS_ACTIVE and prev["status"] != STATUS_ACTIVE:
        return True
    return bool(row.get("review_requested_at")) and not prev["review"]


def mark_notified(conn: Any, settings: SiteSettings, rows: Sequence[Mapping[str, Any]],
                  now: _dt.datetime) -> None:
    t_match = settings.table("matches")
    with conn.cursor() as cur:
        for r in rows:
            cur.execute(f"""UPDATE {t_match}
                               SET notified_at = %(now)s, notified_key = %(nk)s
                             WHERE match_key = %(k)s""",
                        {"now": now, "k": r["match_key"], "nk": notification_key(r)})
    conn.commit()


def manual_queue(conn: Any, settings: SiteSettings, now: Optional[_dt.datetime] = None
                 ) -> list[dict[str, Any]]:
    """The site's manual queue (manual-review rows and rows the gate wants a
    human for), oldest first, with their age in hours."""
    now = now or _utcnow()
    with conn.cursor() as cur:
        out = _queue_rows(cur, settings, now)
    conn.commit()
    return out


def review_match(conn: Any, settings: SiteSettings, key: str, decision: str, *,
                 reviewer: str, note: str = "", tier: Optional[str] = None,
                 now: Optional[_dt.datetime] = None,
                 lock_timeout_s: float = 60.0) -> Optional[dict[str, Any]]:
    """Record a human decision. confirm: active with the proposed (or given)
    tier and its hold; clear: cleared; reopen: hand the row back to the gate.
    Any pending gate review request is resolved by the decision. Waits up to
    ``lock_timeout_s`` for a running sync of the site (SiteLockedError).
    Returns the updated row, or None when the key does not exist."""
    now = now or _utcnow()
    if decision not in ("confirm", "clear", "reopen"):
        raise ValueError("decision must be confirm, clear or reopen")
    if decision != "reopen" and not reviewer.strip():
        raise ValueError("reviewer is required")
    if tier is not None and tier not in BLOCKING_TIERS:
        raise ValueError(f"tier must be one of {BLOCKING_TIERS}")
    t_rec, t_match = settings.table("recalls"), settings.table("matches")
    if getattr(conn, "autocommit", False):
        conn.autocommit = False  # the lock and the update share one transaction
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET LOCAL lock_timeout = {max(1, int(lock_timeout_s * 1000))}")
            try:
                cur.execute("SELECT pg_advisory_xact_lock(%s)",
                            (advisory_lock_id(settings.site),))
            except Exception as e:  # noqa: BLE001 - lock_not_available after the timeout
                raise SiteLockedError(f"site {settings.site}: a recall-gate sync holds the "
                                      f"lock ({type(e).__name__}); try again") from None
            cur.execute(f"""SELECT m.proposed_tier, m.first_matched_at, r.record
                              FROM {t_match} m JOIN {t_rec} r ON r.id = m.recall_id
                             WHERE m.match_key = %s FOR UPDATE OF m""", (key,))
            row = cur.fetchone()
            if row is None:
                conn.rollback()
                return None
            proposed, first, record = row
            params = {"rv": reviewer.strip(), "now": now, "note": note or None, "k": key}
            if decision == "confirm":
                new_tier = tier or proposed
                rec = Recall.from_dict(record if isinstance(record, Mapping)
                                       else json.loads(record))
                rec = with_remedy_options(rec, settings.remedy)
                dec = decide_tier(rec, settings.tier_rules)
                if new_tier != dec.tier:
                    dec = TierDecision(tier=new_tier, rule_id="review",
                                       hold_days=90 if new_tier == TIER_REPAIR_HOLD else None)
                hold = hold_until_for(dec, rec, first_matched=_parse_ts(first))
                cur.execute(f"""UPDATE {t_match} SET status = 'active', tier = %(tier)s,
                                       hold_until = %(hold)s, reviewer = %(rv)s,
                                       reviewed_at = %(now)s, review_note = %(note)s,
                                       stale_since = NULL, review_requested_at = NULL,
                                       gate_proposal = NULL, updated_at = %(now)s
                                 WHERE match_key = %(k)s""",
                            {**params, "tier": new_tier, "hold": hold})
            elif decision == "clear":
                cur.execute(f"""UPDATE {t_match} SET status = 'cleared', reviewer = %(rv)s,
                                       reviewed_at = %(now)s, review_note = %(note)s,
                                       review_requested_at = NULL, gate_proposal = NULL,
                                       updated_at = %(now)s
                                 WHERE match_key = %(k)s""", params)
            else:
                cur.execute(f"""UPDATE {t_match} SET status = 'manual-review', tier = 'review',
                                       hold_until = NULL, reviewer = NULL, reviewed_at = NULL,
                                       review_note = %(note)s, notified_key = NULL,
                                       review_requested_at = NULL, gate_proposal = NULL,
                                       updated_at = %(now)s
                                 WHERE match_key = %(k)s""", params)
            cur.execute(f"""SELECT match_key, match_kind, tier, proposed_tier, status,
                                   hold_until, reviewer, reviewed_at, review_note,
                                   review_requested_at
                              FROM {t_match} WHERE match_key = %s""", (key,))
            out = _rows(cur)[0]
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return out


# ---------------------------------------------------------------------------
# Buy-button checks
# ---------------------------------------------------------------------------

def buy_button_sql_violations(conn: Any, settings: SiteSettings
                              ) -> Optional[list[dict[str, Any]]]:
    """Rows of the site's ``buy_button.sql``: blocked SKUs that its own data
    says still render a buy button. None when not configured. Runs in its
    own READ ONLY transaction, always rolled back (it can change nothing)."""
    sql = str((settings.buy_button or {}).get("sql") or "").strip()
    if not sql:
        return None
    if getattr(conn, "autocommit", False):
        conn.autocommit = False  # READ ONLY needs a transaction block
    conn.rollback()  # start a fresh transaction so READ ONLY is its first statement
    try:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute(f"SET LOCAL statement_timeout = {int(settings.statement_timeout_ms)}")
            cur.execute(sql)
            rows = _rows(cur)
    finally:
        conn.rollback()
    return rows


def _item_url(item: CatalogItem, template: str) -> str:
    """The page to probe: ``url_template`` filled from the row ({asin}
    {product_id} {model_id} {slug}), else its url column. "" (no page) when
    the template names a field the row does not have: an empty {slug} must
    not turn into the listing page."""
    if template:
        values = {"asin": item.asin, "product_id": item.product_id,
                  "model_id": item.model_id, "slug": item.slug}
        try:
            names = {name for _, name, _, _ in string.Formatter().parse(template) if name}
            if any(values.get(n) in (None, "") for n in names):
                return ""
            return template.format(**values)
        except (KeyError, IndexError, ValueError, AttributeError):
            return ""
    return item.url


def _host(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


_REDIRECTS = (301, 302, 303, 307, 308)


def probe_buy_buttons(items: Sequence[CatalogItem], settings: SiteSettings, *,
                      http_get: Optional[HttpGet] = None, user_agent: str = "",
                      allowed_hosts: Optional[Sequence[str]] = None
                      ) -> Optional[dict[str, Any]]:
    """GET each blocked item's live page (read-only) and look for a
    buy-button marker. None when the HTTP probe is not enabled.

    Returns {"checked", "violations", "unverified", "errors"}. ``unverified``
    lists the keys of blocked items nobody could check: no page URL, a host
    that is not the site's, a network error, an HTTP status other than 200,
    404 or 410, or past ``max_probes``. Only ``allowed_hosts`` (the site
    registry's domain and ``buy_button.http.allowed_hosts``) are fetched, and
    redirects are never followed off the site: a redirect to a marker (an
    Amazon /dp/ link) is a live buy button, any other off-site redirect means
    the page is gone. No allowed host at all: nothing is fetched."""
    hcfg = (settings.buy_button or {}).get("http") or {}
    if not hcfg.get("enabled"):
        return None
    items = list(items)
    markers = [re.compile(str(p)) for p in (hcfg.get("markers") or [])]
    if not markers:
        return {"checked": 0, "violations": [], "unverified": [it.key for it in items],
                "errors": ["no buy-button markers configured"]}
    hosts = {h.strip().lower() for h in (allowed_hosts or ()) if h and h.strip()}
    hosts |= {str(h).strip().lower() for h in (hcfg.get("allowed_hosts") or [])
              if str(h).strip()}
    get: HttpGet = http_get or (lambda url, *, headers, timeout: urllib_get(
        url, headers=headers, timeout=timeout, follow_redirects=False))
    headers = {"User-Agent": user_agent or DEFAULTS["http"]["user_agent"],
               **{str(k): str(v) for k, v in (hcfg.get("headers") or {}).items()}}
    max_probes = max(0, int(hcfg.get("max_probes", 50)))
    max_redirects = max(0, int(hcfg.get("max_redirects", 3)))
    timeout = float(hcfg.get("timeout_s", 20))
    checked = 0
    violations: list[dict[str, Any]] = []
    unverified: list[str] = [it.key for it in items[max_probes:]]
    errors: list[str] = []
    if unverified:
        errors.append(f"{len(unverified)} blocked item(s) past max_probes={max_probes} not checked")
    if not hosts:
        errors.append("no allowed host: register the site's domain or set "
                      "buy_button.http.allowed_hosts")
        return {"checked": 0, "violations": [], "errors": errors,
                "unverified": [it.key for it in items]}
    for it in items[:max_probes]:
        url = _item_url(it, str(hcfg.get("url_template") or ""))
        if not url.startswith(("https://", "http://")):
            errors.append(f"{it.key}: no page URL")
            unverified.append(it.key)
            continue
        if _host(url) not in hosts:
            errors.append(f"{it.key}: {_host(url) or 'no host'} is not the site's host; not fetched")
            unverified.append(it.key)
            continue
        verdict = ""
        for _ in range(max_redirects + 1):
            try:
                resp = get(url, headers=headers, timeout=timeout)
            except (urllib.error.URLError, OSError, ValueError) as e:
                errors.append(f"{it.key}: {type(e).__name__}")
                verdict = "unverified"
                break
            if resp.status in _REDIRECTS:
                target = urllib.parse.urljoin(url, str(resp.headers.get("location") or ""))
                hit = next((m.pattern for m in markers if m.search(target)), None)
                if hit:
                    violations.append({"key": it.key, "url": url, "label": it.display,
                                       "marker": f"redirect to {hit}"})
                    verdict = "checked"
                    break
                if _host(target) in hosts and target != url:
                    url = target
                    continue
                verdict = "checked"  # the page leaves the site: no buy button on it
                break
            if resp.status in (404, 410):
                verdict = "checked"
                break
            if resp.status != 200:
                errors.append(f"{it.key}: HTTP {resp.status}")
                verdict = "unverified"
                break
            body = resp.body.decode("utf-8", errors="replace")
            hit = next((m.pattern for m in markers if m.search(body)), None)
            if hit:
                violations.append({"key": it.key, "url": url, "label": it.display,
                                   "marker": hit})
            verdict = "checked"
            break
        if verdict == "checked":
            checked += 1
        else:
            if not verdict:
                errors.append(f"{it.key}: more than {max_redirects} redirects")
            unverified.append(it.key)
    return {"checked": checked, "violations": violations, "unverified": unverified,
            "errors": errors}


# ---------------------------------------------------------------------------
# Email rendering
# ---------------------------------------------------------------------------

def _esc(v: Any) -> str:
    return html.escape("" if v is None else str(v))


def render_match_email(site: str, row: Mapping[str, Any], *, prefix: str = "[recall-gate]",
                       today: Optional[_dt.date] = None) -> tuple[str, str]:
    """(subject, html) for one match row from ``pending_notifications``."""
    from framework.core import email_templates as et
    today = today or _utcnow().date()
    tier, status, kind = str(row.get("tier")), str(row.get("status")), row.get("match_kind")
    blocking = is_blocking(tier, status, row.get("hold_until"), today)
    ikey = item_key(row.get("model_id"), row.get("product_id"))
    label = str(row.get("item_label") or ikey)
    rkind = "warning" if row.get("kind") == KIND_WARNING else "recall"
    prop_raw = row.get("gate_proposal")
    if isinstance(prop_raw, (str, bytes)):
        prop_raw = _json_norm(prop_raw)
    proposal: Mapping[str, Any] = prop_raw if isinstance(prop_raw, Mapping) else {}
    requested = bool(row.get("review_requested_at"))
    why = str(proposal.get("why") or "")
    if requested:
        headline = "REVIEW REQUESTED"
    else:
        headline = tier.upper() if kind == MATCH_EXACT else f"REVIEW ({kind})"
    subject = (f"{prefix} {site}: {headline} - {label[:80]} / CPSC {rkind} "
               f"{row.get('number') or row.get('recall_id')}")
    found = (f"{proposal.get('match_kind')} via {proposal.get('rule')}: {proposal.get('reason')}"
             if proposal.get("match_kind") else str(proposal.get("reason") or ""))
    human_raw = proposal.get("human_decision")
    human: Mapping[str, Any] = human_raw if isinstance(human_raw, Mapping) else {}
    if requested and why == WHY_ESCALATED:
        verb = "cleared" if human.get("status") == STATUS_CLEARED else "confirmed"
        until = f" until {human.get('hold_until')}" if human.get("hold_until") else ""
        decided = (f"{human.get('reviewer')} {verb} it ({human.get('match_kind')}, "
                   f"{human.get('tier')}{until})" if human else "A reviewer decided it")
        rose = (f" The recall's own tier rose from {proposal.get('recall_tier_was')} to "
                f"{proposal.get('recall_tier')}." if proposal.get("recall_tier") else "")
        box = et.callout("error" if blocking else "warn",
                         "The gate blocked this product over a review decision",
                         _esc(f"{decided}; the gate now finds {found or 'a stricter match'}."
                              f"{rose} The gate always applies a stricter block by itself"
                              + (" and the buy button is hidden now" if blocking else "")
                              + ". Confirm keeps it, clear lifts it."))
    elif requested and why == WHY_STRONGER_AFTER_CLEAR:
        box = et.callout("warn", "A match you cleared has stronger evidence",
                         _esc(f"Cleared by {proposal.get('cleared_by')} as "
                              f"{proposal.get('cleared_kind')}; the gate now finds {found}. It is "
                              "back in the manual queue: kept out of picks, buy button "
                              "unchanged until you confirm or clear it again."))
    elif requested:
        box = et.callout("warn", "The gate would lift this match - it will not do that itself",
                         _esc(("Still blocked. " if blocking else "")
                              + "The stored decision is unchanged; the gate now finds: "
                              + (found or "no match") + ". Confirm keeps it, clear lifts it."))
    elif blocking:
        box = et.callout("error", "Buy button blocked",
                         _esc("The site's buy-button gate (v_recall_buy_blocks) now hides this "
                              "product's buy button."))
    elif kind == MATCH_EXACT:
        box = et.callout("warn", "Recall notice only",
                         _esc(f"The repair hold ended {row.get('hold_until')}; the product keeps "
                              "its buy button with a recall notice."))
    else:
        box = et.callout("info", "Manual review needed",
                         _esc("Not blocked automatically. Confirm or clear it; until then the "
                              "product is kept out of picks and ladders."))
    link = (f"<a href='{_esc(row.get('url'))}'>{_esc(row.get('title'))}</a>"
            if row.get("url") else _esc(row.get("title")))
    ev_raw = row.get("evidence")
    evidence: Mapping[str, Any] = ev_raw if isinstance(ev_raw, Mapping) else {}
    tier_text = (f"{_esc(tier)} / {_esc(status)}"
                 + (f" until {_esc(row.get('hold_until'))}" if row.get("hold_until") else "")
                 + (f" (proposed: {_esc(row.get('proposed_tier'))})" if kind != MATCH_EXACT
                    else ""))
    rows = [
        ["Site", _esc(site)],
        ["Product", f"{_esc(label)} <code>{_esc(ikey)}</code>"],
        [f"CPSC {rkind}", f"{_esc(row.get('number'))} ({_esc(row.get('published_at'))}): {link}"],
        ["Match", f"{_esc(kind)} via {_esc(evidence.get('rule', ''))}: {_esc(row.get('reason'))}"],
        ["Tier", tier_text],
        ["Hazard", _esc(row.get("hazard"))],
        ["Remedy", _esc(row.get("remedy"))],
        ["Units", _esc(row.get("units"))],
    ]
    cmd = (f"python3 -m framework.core.recall_gate review --site {_esc(site)} --match-key "
           f"'{_esc(row.get('match_key'))}' --decision confirm|clear --reviewer &lt;you&gt;")
    body = et.page(title=_esc(subject), body_parts=[
        et.header_bar("Recall match", sublabel=site),
        box,
        et.table(["Field", "Value"], rows),
        et.section_h2("Review"),
        "<p style='font-size:13px'>Record a decision (the gate never overwrites it):"
        f"<br><code>{cmd}</code></p>",
    ])
    return subject, body


def render_summary_email(site: str, rows: Sequence[Mapping[str, Any]], *,
                         prefix: str = "[recall-gate]") -> tuple[str, str]:
    from framework.core import email_templates as et
    subject = f"{prefix} {site}: {len(rows)} more recall matches"
    table_rows = [[_esc(r.get("match_kind")), _esc(r.get("tier")), _esc(r.get("item_label")),
                   _esc(r.get("number")), _esc(r.get("title"))] for r in rows]
    body = et.page(title=_esc(subject), body_parts=[
        et.header_bar("More recall matches", sublabel=site),
        et.table(["Match", "Tier", "Product", "CPSC #", "Title"], table_rows),
    ])
    return subject, body


def render_buy_button_alert(site: str, violations: Sequence[Mapping[str, Any]], *,
                            prefix: str = "[recall-gate]",
                            unverified: Sequence[Mapping[str, Any]] = ()) -> tuple[str, str]:
    """(subject, html): blocked SKUs that still show a buy button, plus the
    blocked SKUs nobody could check (counted as live until checked)."""
    from framework.core import email_templates as et
    parts = []
    if violations:
        parts.append(f"{len(violations)} recalled SKU(s) still show a buy button")
    if unverified:
        parts.append(f"{len(unverified)} blocked SKU(s) could not be verified")
    subject = f"{prefix} {site}: " + "; ".join(parts or ["buy-button check"])
    rows = [[_esc(v.get("label") or v.get("key")), _esc(v.get("url") or ""),
             _esc(v.get("marker") or v.get("source") or "")] for v in violations]
    body_parts = [et.header_bar("Recalled SKU with a live buy button", sublabel=site)]
    if violations:
        body_parts += [
            et.callout("error", "Stop publishing until fixed",
                       _esc("These products have an active stop-use or repair-hold match but "
                            "the live page (or the site's own data) still offers a buy "
                            "button.")),
            et.table(["Product", "Page", "Evidence"], rows)]
    if unverified:
        body_parts += [
            et.callout("warn", "Not verified",
                       _esc("Nobody could check these blocked products (no check configured, "
                            "no page URL, an off-site host, an HTTP error or past max_probes). "
                            "They count as live buy buttons until a check passes.")),
            et.table(["Product", "Why"], [[_esc(u.get("label") or u.get("key")),
                                           _esc(u.get("why") or "")] for u in unverified])]
    body = et.page(title=_esc(subject), body_parts=body_parts)
    return subject, body


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def connect_site(dsn: str, settings: SiteSettings) -> Any:
    """psycopg2 connection to a site database (TCP keepalives, a connect
    timeout and an application_name the site's DBA can see)."""
    from framework.core.pg_conn import connect
    return connect(dsn, connect_timeout=settings.connect_timeout_s,
                   application_name="recall-gate")


def _cli_settings(args: argparse.Namespace) -> SiteSettings:
    cfg = resolve_config(use_storage=not args.no_storage)
    if args.site not in cfg["by_site"]:
        raise LookupError(f"unknown site {args.site!r} (no by_site entry)")
    settings, problems = site_settings(cfg, args.site)
    if problems:
        raise RecallGateConfigError("; ".join(problems))
    return settings


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="python3 -m framework.core.recall_gate",
                                description="CPSC recall gate (deterministic; no LLM).")
    p.add_argument("--no-storage", action="store_true",
                   help="use the repo config only (do not read framework storage)")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("schema", help="print the reference DDL for a site database")
    s.add_argument("--site", help="use this site's table names")
    s = sub.add_parser("config", help="print the resolved config (no secrets)")
    s.add_argument("--site")
    s = sub.add_parser("fetch", help="fetch and parse the CPSC sources (no database)")
    s.add_argument("--source", action="append", choices=SOURCES)
    s.add_argument("--out", help="write the merged records as JSON")
    s = sub.add_parser("match", help="offline dry run of the matcher")
    s.add_argument("--site", required=True)
    s.add_argument("--recalls", required=True, help="JSON list of records (fetch --out)")
    s.add_argument("--catalog", required=True, help="JSON list of catalog rows")
    s.add_argument("--today", help="evaluate holds as of this date (YYYY-MM-DD)")
    s = sub.add_parser("queue", help="list the site's manual-review queue")
    s.add_argument("--site", required=True)
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("review", help="record a human decision on one match")
    s.add_argument("--site", required=True)
    s.add_argument("--match-key", required=True)
    s.add_argument("--decision", required=True, choices=("confirm", "clear", "reopen"))
    s.add_argument("--reviewer", default="")
    s.add_argument("--note", default="")
    s.add_argument("--tier", choices=BLOCKING_TIERS)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    try:
        if args.cmd == "schema":
            print(schema_sql(_cli_settings(args).tables if args.site else None))
            return 0
        if args.cmd == "config":
            cfg = resolve_config(use_storage=not args.no_storage)
            if args.site:
                if args.site not in cfg["by_site"]:
                    print(f"unknown site {args.site!r}", file=sys.stderr)
                    return 1
                settings, problems = site_settings(cfg, args.site)
                doc: Any = {"site": args.site, "problems": problems,
                            "dsn_env_names": resolve_dsn_env_names(
                                settings, use_storage=not args.no_storage),
                            "entry": cfg["by_site"][args.site]}
            else:
                doc = {**cfg, "problems": validate_config(cfg)}
            print(json.dumps(doc, indent=2, sort_keys=True, default=str))
            return 0
        if args.cmd == "fetch":
            cfg = resolve_config(use_storage=not args.no_storage)
            if args.source:
                cfg = _merge(cfg, {"sources": {n: {"enabled": n in args.source}
                                               for n in SOURCES}})
            results = SourceFetcher(cfg).fetch_all({})
            for name, result in results.items():
                print(f"{name}: {'ok' if result.ok else 'FAILED'} "
                      f"records={len(result.records)} {result.error}".rstrip())
            merged = merge_sources(results, cfg.get("remedy_options"))
            if args.out:
                Path(args.out).write_text(json.dumps(
                    [rec.to_dict() for rec in merged.values()], indent=1, ensure_ascii=False))
            print(f"merged records: {len(merged)}")
            return 0 if all(result.ok for result in results.values()) else 2
        if args.cmd == "match":
            settings = _cli_settings(args)
            recs = [with_remedy_options(Recall.from_dict(d), settings.remedy)
                    for d in json.loads(Path(args.recalls).read_text())]
            items, problems = catalog_from_rows(json.loads(Path(args.catalog).read_text()))
            for pr in problems:
                print(f"catalog: {pr}", file=sys.stderr)
            today = _as_date(args.today) or _utcnow().date()
            as_of = _dt.datetime.combine(today, _dt.time(12), tzinfo=_dt.timezone.utc)
            matcher = Matcher(settings, items)
            out = []
            for rec in recs:
                cats, found = matcher.match(RecallView(rec), now=as_of)
                for m in found:
                    out.append({"recall": rec.id, "number": rec.number,
                                "item": m.item.display, "match_key": m.key,
                                "kind": m.match_kind, "rule": m.rule, "tier": m.tier,
                                "status": m.status, "hold_until": m.hold_until,
                                "blocking": m.blocking(today), "reason": m.reason,
                                "categories": list(cats)})
            print(json.dumps(out, indent=2))
            return 0
        settings = _cli_settings(args)
        # The site registry's storage override is where a new site's dsn_env
        # lives, so the CLI reads it exactly like the agent does.
        dsn, names = resolve_dsn(settings, use_storage=not args.no_storage)
        if not dsn:
            print(f"no DSN: none of {names} is set", file=sys.stderr)
            return 2
        conn = connect_site(dsn, settings)
        try:
            if args.cmd == "queue":
                rows = manual_queue(conn, settings)
                if args.json:
                    print(json.dumps(rows, indent=2, default=str))
                else:
                    for q in rows:
                        print(f"{q['age_h']:>7}h  {q['match_kind']:<9} {q['proposed_tier']:<11} "
                              f"{q['item_label']}  <- {q['number']} {str(q['title'])[:70]}\n"
                              f"          {q['match_key']}\n          {q['reason']}")
                    print(f"{len(rows)} in queue")
                return 0
            row = review_match(conn, settings, args.match_key, args.decision,
                               reviewer=args.reviewer, note=args.note, tier=args.tier)
            if row is None:
                print(f"no match {args.match_key!r}", file=sys.stderr)
                return 1
            print(json.dumps(row, indent=2, default=str))
            return 0
        finally:
            conn.close()
    except LookupError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except (RecallGateConfigError, SiteLockedError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
