"""Compatibility graph: which products work together, how, under which
assumptions, and on whose word.

WHY THIS EXISTS
---------------
A catalog site earns when a buyer adds the right second product: the panel
that charges their station, the battery that expands it, the filter that fits
their robot. A wrong "works with" claim costs more than the sale, and for
solar inputs it can destroy equipment. A panel's open-circuit voltage (Voc)
RISES as it gets colder, so a string that is inside a power station's input
limit on the spec sheet (25 °C) can exceed it on a clear winter morning,
which is exactly when outage buyers use it. A design that applied the
temperature coefficient at -20 °C with no margin (x1.126) was rejected in
review: NEC Table 690.7(A) goes up to x1.25 at -40 °C and many panels have
steeper coefficients than the -0.28 %/°C it assumed.

This primitive turns manufacturer specs (normalised by
framework.core.spec_sources, which never parses Amazon text) into EDGES
between models. Every edge carries a verdict, the wiring that works, the
assumptions shown next to it on the page, the sources it rests on and the
rule versions that produced it.

The code is niche-agnostic. It knows rule KINDS (cold open-circuit voltage,
minimum voltage, current, connector paths, manufacturer fit lists, watts
clipping), configurations of N identical units, provenance and versioning.
Which spec fields feed a rule, which product kinds a relation joins, the
connector taxonomy and the exclusions are configuration (a niche PROFILE).

RULE KINDS (rules are versioned data: config ``rules.<id>``)
------------------------------------------------------------
``manufacturer_list``  Fit statements published by a manufacturer
                       (:class:`FitRecord`, e.g. "compatible with ...",
                       "fits models ..."). PREFERRED whenever present; also
                       the whole rule for model-keyed consumable fitment
                       (filters, brushes, bags -> robot models). Operator
                       entries (origin "manual") outrank manufacturers.
``max_voltage_cold``   n x Voc x factor must stay at least ``headroom``
                       (>= 5 %, a code floor) under the input's maximum.
                       factor = 1 + tc x (Tmin - 25) with the panel's
                       published Voc temperature coefficient (%/°C); when tc
                       is unknown, the NEC 690.7(A) factor for Tmin (up to
                       x1.25 at -40 °C); a tc outside the plausible band is
                       distrusted (the larger of its factor and NEC's is
                       used; below the NEC table it is unknown, never used
                       alone). Tmin is -40 °C, or a per-ZIP extreme minimum
                       from config that itself defaults to -40. Within the
                       headroom band the verdict is ``over_limit``: we never
                       call a string that close to the limit a fit.
``min_voltage``        n x Vmp must reach the input's minimum voltage.
``max_current``        m x Isc (x isc_factor) must not exceed the input's
                       maximum current, unless the input documents current
                       limiting.
``connector_path``     A direct connector match (taxonomy ids, plus declared
                       ``mates_with``), or exactly ONE adapter whose
                       connectors come from manufacturer data (spec values
                       sourced from manufacturer pages/manuals/FAQs; never
                       Amazon, never operator entry). Connector text must
                       name each connector specifically: a piece naming two
                       connectors, or "requires ... adapter", resolves to
                       nothing, and generic wording ("PV connector") counts
                       only as a per-side fallback. Connector and adapter
                       ratings, when known, must cover the configuration's
                       cold voltage and current; when the ruleset computes
                       those but a value is missing, the rating check is
                       unknown. B's own connector rating is checked even when
                       the path is unknown (B's connector, and a branch
                       connector of its type, carry the string voltage and
                       the array current whatever the path turns out to be).
                       The taxonomy's ``series``/``parallel`` flags on B's own
                       connector say which multi-unit wirings exist at all
                       (barrel-plug panels cannot be wired in series), also
                       when A's connector is unknown; parallel strings need a
                       branch connector, which counts against the one-adapter
                       budget, so a path through an adapter offers none.
``watts_clip``         Informational: array watts vs the input's maximum.

A missing input never passes a check: the check is ``unknown`` and so is the
verdict (``on_missing: "skip"`` may relax a non-voltage kind, labelled as an
assumption). The cold-voltage check can never be relaxed. A spec field still
in conflict in spec_sources is not ACCEPTED, so it is missing here too. When a
fit statement decides the verdict instead, every check that could not run is
named next to it ("Input current not checked: ...") and nothing is
extrapolated past it (see PRECEDENCE AND VERSIONS).

Policy exclusions (config ``policy``: excluded connectors such as year-1
240 V cords, model filters) produce no edge (a :class:`Skip`), and
:meth:`CompatGraph.plan` deletes any row the pair had.

VERDICTS (stored in compat_edges.verdict)
-----------------------------------------
``fits``                one unit works directly;
``fits_with_adapter``   works through one adapter (``via_model_id``); this label
                        wins over the series/parallel ones, and
                        ``config.series``/``config.parallel`` still say how
                        many units;
``fits_in_series``      needs >= 2 units in series (one alone is too low);
``fits_in_parallel``    only parallel configurations work (only a statement
                        can say so: parallel strings add current, so the
                        computed rules never need them);
``over_limit``          exceeds a maximum (or the cold-voltage headroom);
``no_fit``              cannot work (too low in every configuration, or a
                        manufacturer says so);
``unknown``             not enough data. Not stored by default.

``config.series`` / ``config.parallel`` describe the RECOMMENDED
configuration: the one with the most usable watts, then the fewest units.
``config.options`` lists every configuration evaluated, and
``config.assumptions`` the human-readable assumptions behind the verdict.

PRECEDENCE AND VERSIONS
-----------------------
manual record > manufacturer records > computed rules. Computed rules still
run next to a statement, for the configuration the edge would store (the
record's own series/parallel, evaluated even outside the enumeration, or
the best-supported one it covers):

* SAFETY checks (cold voltage with its headroom, connector/adapter ratings)
  are a floor no statement overrides, whatever ``on_disagreement`` says. A
  stated configuration that fails one is replaced by the best smaller one
  that passes (flag ``record-config-limited``), a stated max_series /
  max_parallel / max_units is cut to what passes, and when nothing the
  statement covers passes the verdict is ``over_limit``.
* Other disagreements (minimum voltage, current, connector path) follow
  ``on_disagreement``: ``prefer_manufacturer`` (default; also how manual
  records are treated) keeps the statement, ``most_conservative`` lets the
  computed result stand, ``unknown`` withholds the verdict.
* Every disagreement adds the flag ``computed-check-disagrees`` and an
  assumption saying why. A safety check that could not run (no coefficient
  on thin film, a missing Voc / Isc behind a rated connector) adds the flag
  ``computed-check-unknown`` and a "not checked" assumption in place of the
  design-temperature line. Any other check that could not run (the input
  current, the minimum voltage, the connection path) adds the flag
  ``computed-check-incomplete`` and a "not checked" assumption naming the
  value that is missing. Renderers must surface all three flags.
* A statement vouches for the configuration it names (a plain "fits":
  one unit; "fits_in_series": two in series; "fits_in_parallel": two
  strings). Nothing is extrapolated past a check that failed or could not
  run: the stored configuration takes the fewest units beyond those the
  statement names (for a safety check that could not run: the fewest at
  all) along the axis such a check depends on (current and the connection
  path: parallel strings; cold voltage, a voltage rating, or a connection
  whose B connector is unknown: units in series), and ``max_series`` / ``max_parallel`` /
  ``max_units`` / ``min_series`` count only configurations whose unchecked
  values are no worse than at the statement's own (fewer units in series
  can miss an unknown minimum voltage). So for anything the statement does
  not name, missing data never yields a larger configuration or limit than
  the same pair with the data published. A configuration the record names
  itself stands unless a check that ran proves it unsafe; when a safety
  check could not run it is shown with ``computed-check-unknown``.
* A plain "fits" stored as a series string while one unit alone fails is
  ``fits_in_series``; a computed adapter path makes it ``fits_with_adapter``.
  A non-fit verdict keeps only a ``via`` the record itself names.

A RULESET (config ``rulesets.<id>``) joins two product kinds under one
relation and lists the rules that decide it. One edge per (a, b, relation,
ruleset). ``rule_version`` is the composite of the ruleset's version, each
member rule's version and :data:`ENGINE_VERSION`; bumping any of them makes
every edge of that ruleset stale, and :meth:`CompatGraph.plan` rewrites
them. ``config.inputs_hash`` covers the inputs and effective parameters, so
changed specs are rewritten too and unchanged edges are not touched.

CONFIG (lowest to highest precedence)
-------------------------------------
    DEFAULTS (this module: conservative electrical defaults, NEC table)
      <- file["defaults"]
      <- file["profiles"][<profile>]   niche library: connectors, rules,
                                       rulesets, policy, texts
      <- file["by_site"][<site>]
      <- the caller's site_cfg
      <- file["by_agent_id"][<agent id>]

``file`` is the storage config ``config/compat-graph-config.json``, else the
repo default of the same name. Dicts deep-merge and lists replace (rules,
rulesets and connectors are dicts keyed by id, so one layer can bump one
rule's version). Safety floors are code: headroom never below 5 %, the default
design temperature never warmer than -40 °C, NEC factors never below the
code table (nor below a warmer band), at most one adapter (a parallel branch
connector counts), no stated fit stored with a configuration that fails a
safety check.

Inputs never rest on Amazon: values whose source URL is Amazon's are dropped
when a :class:`ModelSpec` is built, and spec_values rows without an
explicit ``accepted`` status are ignored.

INERT BY DESIGN
---------------
Importing this module does nothing. It reads config (read-only), makes no
HTTP requests and no LLM calls. The only writer is :class:`EdgeWriter`, which
writes through a DB-API connection the CALLER opens (a site agent) and
commits once per plan (``apply(plan, commit=False)`` leaves the transaction
to the caller). Manual rows are protected in the plan and in the SQL.

TYPICAL CALLER (a spec-extractor agent's compat phase)
------------------------------------------------------
    graph = CompatGraph(resolve_config(site=site, agent_id=agent_id))
    nodes = [ModelSpec.from_db_rows(m.id, m.kind, rows_by_model[m.id], name=m.name,
                                    brand=m.brand) for m in models]
    result = graph.build(nodes, fit_records)          # pure
    writer = EdgeWriter(conn, cfg=graph.cfg)
    plan = graph.plan(result, writer.load_existing(rule_ids=result.rulesets))
    writer.apply(plan)                                # upserts + deletes
    metrics = result.metrics()

A full graph is held in memory (about 9 KB per evaluated pair). For a big
catalog use ``graph.iter_build(nodes, fit_records, batch_size=25)`` and plan
and apply each batch against the existing rows of that batch's A models;
after models change, ``build(..., only_models=changed_ids)``. A partial
result only ever deletes rows inside the part it covered. Stored edges are
computed at the default (coldest) design temperature: ``plan`` refuses a
build at a warmer per-ZIP or caller climate. Rows of disabled or removed
rulesets are listed in ``plan.orphaned`` and deleted only with
``plan(..., purge_orphaned=True)``.

CLI (offline, read-only): ``python3 -m framework.core.compat_graph --help``
"""
from __future__ import annotations

import argparse
import collections
import functools
import json
import logging
import math
import re
import sys
from dataclasses import dataclass, field as dc_field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence, Union

from framework.core import spec_sources as ss
from framework.core.short_circuit import stable_hash

log = logging.getLogger("framework.compat_graph")

CONFIG_KEY = "config/compat-graph-config.json"
_REPO_CONFIG = (Path(__file__).resolve().parents[2]
                / "config" / "compat-graph-config.json")

# Bump when evaluation semantics change in code: every edge's rule_version
# changes with it, so all edges are recomputed (plan() leaves an edge alone
# when only its stored configuration would change, so a semantic change
# without a bump would leave old configurations in place).
# 2: stated fits never stored with a configuration that fails a safety check,
#    unknown connector ratings, the branch-connector budget, NEC rows that
#    can only raise a factor, strict connector text. Version 1 never shipped.
# 3: stated fits never extrapolated past a check that failed or could not run
#    (current, connection path, minimum voltage): fewest units on its axis,
#    limits only where it is no worse than at the statement's configuration,
#    "not checked" assumptions and the computed-check-incomplete flag; B's own
#    connector decides wiring and its rating is checked when A's is unknown.
# 4: connector text (taxonomy aliases and policy exclusion scans) and policy
#    "matches" rules read Unicode hyphens and dashes (U+2010..U+2015) as "-":
#    "NEMA L14–30P" with an en dash resolves, and is excluded, like
#    "NEMA L14-30P".
ENGINE_VERSION = "4"

# Verdicts (compat_edges.verdict).
FITS = "fits"
FITS_WITH_ADAPTER = "fits_with_adapter"
FITS_IN_SERIES = "fits_in_series"
FITS_IN_PARALLEL = "fits_in_parallel"
OVER_LIMIT = "over_limit"
NO_FIT = "no_fit"
UNKNOWN = "unknown"
VERDICTS = (FITS, FITS_WITH_ADAPTER, FITS_IN_SERIES, FITS_IN_PARALLEL, OVER_LIMIT, NO_FIT, UNKNOWN)
FIT_VERDICTS = frozenset({FITS, FITS_WITH_ADAPTER, FITS_IN_SERIES, FITS_IN_PARALLEL})
FAIL_VERDICTS = frozenset({OVER_LIMIT, NO_FIT})
# Most conservative first: used when statements disagree.
_CONSERVATIVE_ORDER = (NO_FIT, OVER_LIMIT, UNKNOWN, FITS_IN_PARALLEL, FITS_IN_SERIES,
                       FITS_WITH_ADAPTER, FITS)

# Edge confidence (compat_edges.confidence) and who decided the verdict.
CONF_MANUAL = "manual"
CONF_MANUFACTURER = "manufacturer-listed"
CONF_COMPUTED = "computed"
CONFIDENCES = (CONF_MANUAL, CONF_MANUFACTURER, CONF_COMPUTED)
DECIDED_MANUAL = "manual"
DECIDED_MANUFACTURER = "manufacturer_list"
DECIDED_COMPUTED = "computed"

ORIGIN_MANUFACTURER = "manufacturer"
ORIGIN_MANUAL = "manual"
RECORD_ORIGINS = (ORIGIN_MANUFACTURER, ORIGIN_MANUAL)

# Check outcomes.
PASS = "pass"
FAIL = "fail"
UNKNOWN_OUTCOME = "unknown"
INFO = "info"
SKIPPED = "skipped"

KIND_MANUFACTURER_LIST = "manufacturer_list"
KIND_MAX_VOLTAGE_COLD = "max_voltage_cold"
KIND_MIN_VOLTAGE = "min_voltage"
KIND_MAX_CURRENT = "max_current"
KIND_CONNECTOR_PATH = "connector_path"
KIND_WATTS_CLIP = "watts_clip"
RULE_KINDS = (KIND_MANUFACTURER_LIST, KIND_MAX_VOLTAGE_COLD, KIND_MIN_VOLTAGE,
              KIND_MAX_CURRENT, KIND_CONNECTOR_PATH, KIND_WATTS_CLIP)
_COMPUTED_KINDS = frozenset(RULE_KINDS) - {KIND_MANUFACTURER_LIST}
# Failing one of these means "exceeds a maximum" (over_limit), not "no fit".
_MAX_KINDS = frozenset({KIND_MAX_VOLTAGE_COLD, KIND_MAX_CURRENT})
# A connector rating that could not be checked (the cold voltage or the
# design current is unknown): a safety check that did not run.
_RATING_UNKNOWN_CODES = frozenset({"rating-voltage-unknown", "rating-current-unknown"})
# Non-safety checks that can be unknown, and how a "not checked" assumption
# names them (the reason names the missing value: texts "role.<side>.<role>").
_NOT_CHECKED_WHAT = {KIND_MAX_CURRENT: "Input current", KIND_MIN_VOLTAGE: "Minimum input voltage",
                     KIND_CONNECTOR_PATH: "Connector"}
# Problem-code prefixes of a missing or unreadable value (see CompatGraph._number).
_VALUE_PROBLEMS = {"missing": "is not published", "not-a-number": "is not a number",
                   "unit": "is in a unit that cannot be converted", "unmapped": "is not mapped"}
# Evaluation order inside one configuration (connector ratings need the
# configuration's cold voltage and current).
_KIND_ORDER = {KIND_MAX_VOLTAGE_COLD: 0, KIND_MIN_VOLTAGE: 1, KIND_MAX_CURRENT: 2,
               KIND_WATTS_CLIP: 3, KIND_CONNECTOR_PATH: 4}

# role -> canonical unit; roles are what rule field maps name.
_ROLE_UNITS = {"max_v": "V", "min_v": "V", "voc": "V", "vmp": "V", "max_a": "A",
               "isc": "A", "max_w": "W", "rated_w": "W"}
# kind -> (required a roles, required b roles, optional a roles, optional b roles)
_KIND_ROLES: dict[str, tuple[frozenset, frozenset, frozenset, frozenset]] = {
    KIND_MAX_VOLTAGE_COLD: (frozenset({"max_v"}), frozenset({"voc"}), frozenset(),
                            frozenset({"tc", "tc_mv", "technology"})),
    KIND_MIN_VOLTAGE: (frozenset({"min_v"}), frozenset({"vmp"}), frozenset(), frozenset()),
    KIND_MAX_CURRENT: (frozenset(), frozenset({"isc"}), frozenset({"max_a", "current_limited"}),
                       frozenset()),
    KIND_WATTS_CLIP: (frozenset({"max_w"}), frozenset({"rated_w"}), frozenset(), frozenset()),
    KIND_CONNECTOR_PATH: (frozenset({"connectors"}), frozenset({"connectors"}), frozenset(),
                          frozenset()),
    KIND_MANUFACTURER_LIST: (frozenset(), frozenset(), frozenset(), frozenset()),
}

# ── Safety floors (binding; config may only make these stricter) ──────────
MIN_HEADROOM = 0.05
DEFAULT_DESIGN_TEMP_C = -40.0
NEC_REF_TEMP_C = 25.0
MAX_ADAPTERS = 1
MAX_ARRAY_UNITS = 12
# NEC Table 690.7(A), crystalline and multicrystalline silicon: the factor for
# ambient temperatures from the band's lowest value up to the next band.
# (20, 1.02) is the row "24 to 20 °C"; (-40, 1.25) is "-36 to -40 °C". A
# temperature between two integer rows takes the colder row's factor.
NEC_690_7_A: tuple[tuple[float, float], ...] = (
    (20.0, 1.02), (15.0, 1.04), (10.0, 1.06), (5.0, 1.08), (0.0, 1.10),
    (-5.0, 1.12), (-10.0, 1.14), (-15.0, 1.16), (-20.0, 1.18), (-25.0, 1.20),
    (-30.0, 1.21), (-35.0, 1.23), (-40.0, 1.25),
)
# Comparisons tolerate 1 nV / 1 nA of float noise; nothing physical lives there.
_EPS = 1e-9


class CompatGraphError(Exception):
    """Base error for this module."""


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULTS: dict[str, Any] = {
    "schema_version": "1",
    "profile": "",
    "climate": {
        # Coldest design temperature for edges evaluated without a location
        # (every stored edge). Clamped to DEFAULT_DESIGN_TEMP_C or colder.
        "default_min_c": DEFAULT_DESIGN_TEMP_C,
        # Per-ZIP extreme minimum, °C: {"<zip5>" | "<zip3>": value}. Normally
        # the ASHRAE extreme annual mean minimum dry-bulb temperature (the
        # value NEC 690.7 points to); a zip3 entry must be the COLDEST value
        # within that prefix. Empty: every ZIP uses default_min_c. Location
        # climates are for per-ZIP tools: CompatGraph.plan() refuses to write
        # shared edges computed warmer than default_min_c.
        "zip_min_c": {},
        "zip_source": "",
        "zip3_fallback": True,
        "plausible_c": [-70.0, 30.0],
    },
    "tables": {
        # Extra rows for NEC Table 690.7(A) as [band_low_c, factor]; merged
        # into the code table by band, keeping the LARGER factor. Rows colder
        # than -40 extend the table; nothing can lower a code factor.
        "nec_690_7_a": [],
    },
    # Default parameters per rule kind (a rule's own params override them).
    "kind_defaults": {
        KIND_MAX_VOLTAGE_COLD: {
            "headroom": MIN_HEADROOM,
            # A Voc temperature coefficient outside this band (%/°C) is
            # distrusted: positive values are Isc coefficients or sign slips,
            # near-zero ones are mis-scaled.
            "tc_plausible_pct_per_c": [-0.6, -0.2],
            # Technology text matching these means NEC 690.7(A) (crystalline
            # silicon only) does not apply: without a published tc the
            # verdict is unknown.
            "non_crystalline_patterns": [r"thin[- ]?film", r"\bcigs?\b", r"\bcdte\b",
                                         r"cadmium", r"amorphous", r"\ba-?si\b(?!-)",
                                         r"perovskite", r"organic"],
        },
        KIND_MIN_VOLTAGE: {"vmp_factor": 1.0, "on_missing": "unknown"},
        KIND_MAX_CURRENT: {
            # NEC 690.8(A)(1) sizes PV circuits at 125 % of Isc; set 1.25 to
            # apply it. 1.0 is the plain "m x Isc <= Imax" check.
            "isc_factor": 1.0,
            "on_missing": "unknown",
        },
        KIND_WATTS_CLIP: {"on_missing": "skip"},
        KIND_CONNECTOR_PATH: {
            "max_adapters": MAX_ADAPTERS,
            "adapter_kinds": ["adapter", "cable"],
            "adapter_fields": {"connector_a": "connector_a", "connector_b": "connector_b",
                               "max_a": "max_a", "max_v": "max_v"},
            # An adapter enters the graph only when its connectors come from
            # one of these source kinds (manufacturer fit data). Operator
            # knowledge goes in a manual FitRecord (with ``via``), never here.
            "fit_source_kinds": ["manufacturer-page", "manual-pdf", "manufacturer-faq"],
            # Sides ("a", "b") whose connector text may use a taxonomy entry's
            # ``fallback_aliases`` (generic wording such as "PV connector"),
            # consulted only when nothing specific matched. Empty: nowhere.
            "fallback_alias_sides": [],
            # Relation of FitRecords that endorse an adapter for a product
            # (preferred when several adapters bridge the same connectors).
            "endorse_relation": "",
            # Connectors known on both sides but no documented path.
            "no_path": "unknown",
            "check_ratings": True,
            "on_missing": "unknown",
        },
        KIND_MANUFACTURER_LIST: {
            # prefer_manufacturer | most_conservative | unknown: what happens
            # when a statement and the computed rules disagree. Whatever the
            # policy, a fit is never stored with a configuration that fails
            # the cold-voltage check or a connector rating (a code floor).
            "on_disagreement": "prefer_manufacturer",
            "source_kinds": ["manufacturer-page", "manual-pdf", "manufacturer-faq",
                             "cert-listing"],
        },
    },
    # Connector taxonomy {id: {label, family, aliases: [regex],
    # fallback_aliases: [regex], mates_with: [id], max_a, max_v, series: bool,
    # parallel: bool}}. A piece of connector text that names more than one
    # connector, or matches ``connector_text.ambiguous_patterns``, resolves to
    # nothing (it describes a part the product does not have built in).
    "connectors": {},
    "connector_text": {
        "ambiguous_patterns": [r"\badapters?\b", r"\brequire[sd]?\b", r"\brequiring\b",
                               r"\bconverters?\b", r"\bsold separately\b", r"\bnot included\b",
                               r"\boptional\b"],
    },
    "policy": {
        # Connector ids that never form a path; adapters/cables carrying one
        # are excluded entirely (with every edge they would have).
        "exclude_connectors": [],
        "exclude_connector_model_kinds": ["adapter", "cable"],
        "connector_fields": ["connector", "connectors", "connector_a", "connector_b"],
        # [{field, op: gte|gt|lte|lt|eq|ne|in|matches|truthy, value, kinds}]
        "exclude_models": [],
    },
    "rules": {},
    "rulesets": {},
    # Default for rulesets without their own store_verdicts.
    "store_verdicts": [FITS, FITS_WITH_ADAPTER, FITS_IN_SERIES, FITS_IN_PARALLEL,
                       OVER_LIMIT, NO_FIT],
    # Assumption texts by code; {placeholders} come from the assumption's
    # params. A profile overrides wording (e.g. "station" / "panel").
    "texts": {
        "design-temp-default": "Coldest design temperature {temp_c} °C (default for any location).",
        "design-temp-zip": "Coldest design temperature {temp_c} °C (extreme minimum for ZIP {zip}).",
        "design-temp-caller": "Coldest design temperature {temp_c} °C (set for this check).",
        "voc-tc": ("Open-circuit voltage corrected for the cold with the published temperature "
                   "coefficient {tc} %/°C: x{factor}."),
        "voc-nec": ("No published temperature coefficient: NEC 690.7(A) cold-voltage factor "
                    "x{factor} applied."),
        "voc-tc-rejected": ("Published temperature coefficient {tc} %/°C is outside the plausible "
                            "range: the larger of its factor and the NEC 690.7(A) factor, "
                            "x{factor}, applied."),
        "cell-type-assumed": "Cell type not published: crystalline silicon assumed for the NEC factor.",
        "headroom": ("Counts as a fit only with at least {headroom_pct}% headroom under the "
                     "{max_v} V maximum input voltage (at most {allowed_v} V)."),
        "current-limited": "The input documents current limiting, so extra current is not drawn.",
        "current-checked": ("Short-circuit current {design_a} A checked against the {max_a} A "
                            "maximum input current."),
        "not-checked": "{what} not checked: {reason}.",
        "cold-not-checked": "Cold-weather open-circuit voltage not checked: {reason}.",
        "rating-not-checked": "Connector rating not checked: {reason}.",
        # How a "not checked" reason names a missing value of rule role
        # <role> on side a (the input) or b (the product attached to it).
        "role.a.max_v": "the input's maximum voltage",
        "role.a.min_v": "the input's minimum voltage",
        "role.a.max_a": "the input's maximum current",
        "role.a.current_limited": "a statement that the input limits its current",
        "role.a.max_w": "the input's maximum power",
        "role.a.connectors": "the input's connector",
        "role.b.voc": "the open-circuit voltage",
        "role.b.vmp": "the maximum-power voltage",
        "role.b.isc": "the short-circuit current",
        "role.b.rated_w": "the rated power",
        "role.b.connectors": "the attached product's connector",
        "connector-unresolved": "a connector is named in a way we cannot match to one connector",
        "connector-no-documented-path": ("the connectors do not mate and no documented adapter "
                                         "joins them"),
        "record-config-limited": ("The stated configuration ({stated}) fails our conservative check "
                                  "({detail}); {shown} is shown instead."),
        "record-limit-clamped": ("The source allows up to {stated}; our conservative check allows at "
                                 "most {allowed} ({detail})."),
        "safety-veto": ("Stated as compatible, but every configuration the statement covers exceeds a "
                        "voltage or rating limit at the coldest design temperature, so it is not "
                        "shown as a fit."),
        "per-input": "Limits apply to one input; inputs are not combined.",
        "watts-clipped": "{array_w} W of rated input; the input uses at most {max_w} W.",
        "parallel-connector": "{parallel} strings in parallel need a parallel (branch) connector.",
        "adapter": "Connects through one adapter: {via_name}.",
        "manufacturer-listed": "Listed as compatible by the manufacturer ({publisher}).",
        "manufacturer-not-compatible": "The manufacturer ({publisher}) lists this combination as not compatible.",
        "manual": "Set by an operator: {statement}",
        "computed-disagrees": "Our conservative check disagrees: {detail}",
        "records-disagree": "Manufacturer sources disagree; the most conservative statement is used.",
    },
    "writer": {
        "table": "compat_edges",
        # Edge row name -> column name.
        "columns": {},
        "page_size": 500,
    },
}

_ROW_FIELDS = ("a_model_id", "b_model_id", "relation", "verdict", "config", "via_model_id",
               "rule_id", "rule_version", "evidence_source_ids", "confidence", "computed_at")
_KEY_FIELDS = ("a_model_id", "b_model_id", "relation", "rule_id")


def merge_config(base: Mapping, over: Optional[Mapping]) -> dict:
    """Deep-merge two config layers (dicts merge, lists replace). Same
    semantics as framework.core.spec_sources."""
    return ss.merge_config(base, over)


def load_file_config(storage=None) -> dict:
    """The storage copy when present (it replaces the repo default wholesale),
    else the repo default file, else {}. Read-only."""
    if storage is None:
        try:
            from framework.core.storage import get_storage
            storage = get_storage()
        except (Exception, SystemExit) as e:  # unknown backend raises SystemExit
            log.warning("compat_graph: storage unavailable, repo default config only: %s", e)
            storage = None
    if storage is not None:
        try:
            doc = storage.read_json(CONFIG_KEY)
            if isinstance(doc, dict) and doc:
                return doc
        except Exception as e:  # noqa: BLE001 - fall back to the repo default
            log.warning("compat_graph: storage config unreadable: %s", e)
    try:
        doc = json.loads(_REPO_CONFIG.read_text())
        return doc if isinstance(doc, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def resolve_config(*, agent_id: str = "", site: str = "",
                   site_cfg: Optional[Mapping] = None, storage=None,
                   file_cfg: Optional[Mapping] = None) -> dict:
    """DEFAULTS <- defaults <- profiles[profile] <- by_site[site] <- site_cfg
    <- by_agent_id[agent_id]. Pass ``file_cfg`` to skip the storage read."""
    doc = dict(file_cfg) if file_cfg is not None else load_file_config(storage)
    defaults = doc.get("defaults") if isinstance(doc.get("defaults"), Mapping) else {}
    by_site = (doc.get("by_site") or {}).get(site) if site else None
    by_agent = (doc.get("by_agent_id") or {}).get(agent_id) if agent_id else None
    profile = ""
    for layer in (by_agent, site_cfg, by_site, defaults):
        if isinstance(layer, Mapping) and layer.get("profile"):
            profile = str(layer["profile"])
            break
    problems: list[str] = []
    cfg = merge_config(DEFAULTS, defaults)
    if profile:
        prof = (doc.get("profiles") or {}).get(profile)
        if isinstance(prof, Mapping):
            cfg = merge_config(cfg, prof)
        else:
            problems.append(f"unknown profile {profile!r}")
    for layer in (by_site, site_cfg, by_agent):
        if isinstance(layer, Mapping):
            cfg = merge_config(cfg, layer)
    cfg["profile"] = profile
    cfg["site"] = site
    cfg["agent_id"] = agent_id
    cfg["config_problems"] = problems
    return cfg


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _k(x: Any) -> str:
    """Canonical key for a model id (DB ints and fixture strings compare equal)."""
    return "" if x is None else str(x)


def _fmt(v: Any, nd: int = 2) -> str:
    if v is None:
        return "?"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, (int, float)):
        f = float(v)
        if f.is_integer():
            return str(int(f))
        return f"{f:.{nd}f}".rstrip("0").rstrip(".")
    return str(v)


def _r(v: Optional[float], nd: int = 4) -> Optional[float]:
    return None if v is None else round(float(v), nd)


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _render(template: str, params: Mapping[str, Any]) -> str:
    return template.format_map(_SafeDict({k: _fmt(v) for k, v in params.items()}))


def _as_float(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        f = float(value)
    elif isinstance(value, str):
        try:
            f = float(value.strip().replace(",", ""))
        except ValueError:
            return None
    else:
        return None
    return f if math.isfinite(f) else None


def _truthy(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().casefold()
        if v in ("true", "yes", "y", "1", "supported"):
            return True
        if v in ("false", "no", "n", "0", "not supported"):
            return False
    return None


def _as_list(v: Any) -> list:
    if v is None:
        return []
    if isinstance(v, (list, tuple, set, frozenset)):
        return list(v)
    return [v]


def _norm_id(s: Any) -> str:
    return re.sub(r"\s+", "-", str(s).strip().casefold())


def _alnum(s: str) -> str:
    return re.sub(r"[^0-9a-z]+", "", ss.normalize_text(str(s)).casefold())


# U+2010 hyphen .. U+2015 horizontal bar (NFKC already turns U+2011, the
# non-breaking hyphen, into U+2010; normalize_text folds U+2212). Maker text
# writes "L14–30P" with an en dash while aliases and policy regexes are
# written with "-". The same class spec_sources folds for evidence quotes.
_DASHES_RE = re.compile("[\u2010-\u2015]")


def _fold_dashes(s: str) -> str:
    """Unicode hyphens and dashes as an ASCII '-' (applied after
    ``normalize_text``)."""
    return _DASHES_RE.sub("-", s)


# ---------------------------------------------------------------------------
# NEC 690.7(A) and the cold-voltage factor
# ---------------------------------------------------------------------------

def _parse_table(rows: Any) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for row in rows or []:
        try:
            lo, f = float(row[0]), float(row[1])
        except (TypeError, ValueError, IndexError):
            continue
        if math.isfinite(lo) and math.isfinite(f) and f >= 1.0:
            out.append((lo, f))
    return out


def merged_nec_table(extra_rows: Any = None) -> tuple[tuple[float, float], ...]:
    """The code table with config rows merged in by band, keeping the larger
    factor, then made monotone: a colder band never has a smaller factor than
    a warmer one (open-circuit voltage only rises as it gets colder). Config
    can extend the table below -40 °C or raise it, never lower it; a row
    inserted inside a code band cannot undercut that band either, because
    :func:`nec_factor` also takes the code table's own factor."""
    bands = {lo: f for lo, f in NEC_690_7_A}
    for lo, f in _parse_table(extra_rows):
        bands[lo] = max(f, bands.get(lo, 0.0))
    out: list[tuple[float, float]] = []
    floor = 0.0
    for lo, f in sorted(bands.items(), key=lambda x: -x[0]):
        floor = max(floor, f)
        out.append((lo, floor))
    return tuple(out)


def nec_table_problems(extra_rows: Any = None) -> list[str]:
    """Config rows for NEC Table 690.7(A) that are ignored or overridden (a
    factor below the code table, or below a warmer band): the code factor is
    used instead. Reported, never applied."""
    problems: list[str] = []
    for row in extra_rows or []:
        parsed = _parse_table([row])
        if not parsed:
            problems.append(f"tables.nec_690_7_a row {row!r} ignored (needs [band_low_c, factor >= 1])")
    merged = merged_nec_table(extra_rows)
    for lo, f in _parse_table(extra_rows):
        used = nec_factor(lo, merged)
        if used is not None and used > f + _EPS:
            problems.append(f"tables.nec_690_7_a row [{lo:g}, {f:g}] is below the NEC 690.7(A) "
                            f"factor x{used:g} that applies at {lo:g} °C; x{used:g} is used")
    return problems


def _nec_lookup(t: float, table: Sequence[tuple[float, float]]) -> Optional[float]:
    best: Optional[tuple[float, float]] = None
    for lo, f in table:
        if lo <= t + _EPS and (best is None or lo > best[0]):
            best = (lo, f)
    return best[1] if best else None


def nec_factor(design_temp_c: float, table: Optional[Sequence[tuple[float, float]]] = None
               ) -> Optional[float]:
    """NEC Table 690.7(A) correction factor at ``design_temp_c``: 1.0 at or
    above 25 °C; the row whose band contains the temperature; the colder row
    between integer rows; None below the coldest row.

    With ``table`` (normally :func:`merged_nec_table`), the larger of the code
    table's factor and the table's is used, and below the code table the
    result is never smaller than the code's coldest factor (x1.25): no table
    can lower a factor."""
    t = float(design_temp_c)
    if t >= NEC_REF_TEMP_C - _EPS:
        return 1.0
    code = _nec_lookup(t, NEC_690_7_A)
    if table is None:
        return code
    extra = _nec_lookup(t, table)
    if code is None:
        return None if extra is None else max(extra, NEC_690_7_A[-1][1])
    return code if extra is None else max(code, extra)


def coefficient_factor(tc_pct_per_c: float, design_temp_c: float) -> float:
    """1 + tc x (Tmin - 25), never below 1.0 (tc in %/°C, negative for Voc)."""
    return max(1.0, 1.0 + float(tc_pct_per_c) / 100.0 * (float(design_temp_c) - NEC_REF_TEMP_C))


def _tc_unit_kind(unit: str) -> str:
    u = ss.normalize_text(unit or "").replace(" ", "").casefold()
    u = u.replace("°", "deg").replace("℃", "degc")
    if u in ("", "%/degc", "%/c", "%/k", "%/kelvin", "%perdegc", "pct/degc"):
        return "pct_c"
    if u in ("%/degf", "%/f"):
        return "pct_f"
    if u in ("mv/degc", "mv/c", "mv/k"):
        return "mv_c"
    if u in ("v/degc", "v/c", "v/k"):
        return "v_c"
    if u in ("1/k", "1/degc", "/k", "/degc", "k^-1"):
        return "frac_c"
    return ""


def tc_to_pct_per_c(value: Any, unit: str, voc_v: Optional[float]) -> Optional[float]:
    """A Voc temperature coefficient in %/°C from %/°C, %/K, %/°F, mV/°C,
    V/°C or 1/K. None when the value or unit cannot be read (an absolute
    coefficient also needs Voc)."""
    v = _as_float(value)
    if v is None:
        return None
    kind = _tc_unit_kind(unit)
    if kind == "pct_c":
        return v
    if kind == "pct_f":
        return v * 1.8
    if kind == "frac_c":
        return v * 100.0
    if kind in ("mv_c", "v_c"):
        if not voc_v:
            return None
        volts = v / 1000.0 if kind == "mv_c" else v
        return volts / voc_v * 100.0
    return None


# ---------------------------------------------------------------------------
# Inputs: models (graph nodes) and fit records
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FieldValue:
    """One accepted spec value with its provenance (a spec_values row)."""
    value: Any
    unit: str = ""
    source_url: str = ""
    source_kind: str = ""
    publisher: str = ""
    content_sha256: str = ""
    source_id: Any = None
    snippet: str = ""

    @classmethod
    def coerce(cls, v: Any) -> "FieldValue":
        if isinstance(v, FieldValue):
            return v
        if isinstance(v, Mapping) and "value" in v:
            return cls(value=v.get("value"), unit=str(v.get("unit") or ""),
                       source_url=str(v.get("source_url") or ""),
                       source_kind=str(v.get("source_kind") or ""),
                       publisher=str(v.get("publisher") or ""),
                       content_sha256=str(v.get("content_sha256") or ""),
                       source_id=v.get("source_id"), snippet=str(v.get("snippet") or ""))
        return cls(value=v)

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


def _value_from_candidate(c: "ss.Candidate") -> Any:
    """The same value SpecSet.values() reports for an accepted candidate."""
    if c.components:
        return list(c.components)
    if c.value_num is not None and c.value_text in (None, ""):
        return c.value_num
    if c.value_text in ("true", "false") and c.value_num in (0.0, 1.0):
        return c.value_text == "true"
    return c.value_text


def _amazon_sourced(fv: Any) -> bool:
    return isinstance(fv, FieldValue) and bool(fv.source_url) and ss.is_amazon_url(fv.source_url)


@dataclass(frozen=True)
class ModelSpec:
    """A graph node: one model and its accepted, normalised spec values.

    Values whose source URL is Amazon's are dropped on construction (and
    logged): no edge may rest on Amazon content, so the rules that needed
    them say "unknown"."""
    model_id: Any
    kind: str
    values: Mapping[str, FieldValue] = dc_field(default_factory=dict)
    name: str = ""
    brand: str = ""
    aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        bad = sorted(k for k, fv in self.values.items() if _amazon_sourced(fv))
        if bad:
            log.warning("compat_graph: model %s: values sourced from Amazon dropped: %s",
                        self.model_id, ", ".join(bad))
            object.__setattr__(self, "values",
                               {k: v for k, v in self.values.items() if k not in bad})

    def get(self, field_name: Optional[str]) -> Optional[FieldValue]:
        if not field_name:
            return None
        fv = self.values.get(field_name)
        if fv is None or fv.value is None or (isinstance(fv.value, str) and not fv.value.strip()):
            return None
        return fv

    @property
    def label(self) -> str:
        return self.name or _k(self.model_id)

    @classmethod
    def from_values(cls, model_id: Any, kind: str, values: Mapping[str, Any], *,
                    name: str = "", brand: str = "", aliases: Sequence[str] = (),
                    source: Optional[Mapping[str, Any]] = None) -> "ModelSpec":
        """Plain values (or FieldValue dicts). ``source`` (url, kind,
        publisher, content_sha256, source_id) is applied to plain values."""
        src = dict(source or {})
        out: dict[str, FieldValue] = {}
        for k, v in values.items():
            fv = FieldValue.coerce(v)
            if src and not isinstance(v, (FieldValue, Mapping)):
                fv = FieldValue(value=fv.value, unit=fv.unit,
                                source_url=str(src.get("source_url") or src.get("url") or ""),
                                source_kind=str(src.get("source_kind") or src.get("kind") or ""),
                                publisher=str(src.get("publisher") or ""),
                                content_sha256=str(src.get("content_sha256") or ""),
                                source_id=src.get("source_id"))
            out[str(k)] = fv
        return cls(model_id=model_id, kind=str(kind), values=out, name=name, brand=brand,
                   aliases=tuple(aliases))

    @classmethod
    def from_specset(cls, model_id: Any, kind: str, spec: "ss.SpecSet", *, name: str = "",
                     brand: str = "", aliases: Sequence[str] = (),
                     source_ids: Optional[Mapping[str, Any]] = None,
                     extra: Optional[Mapping[str, Any]] = None) -> "ModelSpec":
        """From framework.core.spec_sources' consolidated SpecSet. Only
        ACCEPTED fields are used: a field in conflict is missing here, so the
        rules that need it say "unknown". ``source_ids`` maps source URL to the
        caller's spec_sources row id; ``extra`` adds values (FieldValue dicts
        keep their provenance)."""
        ids = dict(source_ids or {})
        out: dict[str, FieldValue] = {}
        for fname, fr in spec.fields.items():
            c = fr.accepted
            if fr.status != ss.STATUS_ACCEPTED or c is None:
                continue
            out[fname] = FieldValue(value=_value_from_candidate(c), unit=c.unit or "",
                                    source_url=c.source.url, source_kind=c.source.kind,
                                    publisher=c.source.publisher,
                                    content_sha256=c.source.content_sha256,
                                    source_id=ids.get(c.source.url), snippet=c.snippet)
        for k, v in (extra or {}).items():
            out[str(k)] = FieldValue.coerce(v)
        return cls(model_id=model_id, kind=str(kind), values=out, name=name, brand=brand,
                   aliases=tuple(aliases))

    @classmethod
    def from_db_rows(cls, model_id: Any, kind: str, rows: Iterable[Mapping[str, Any]], *,
                     name: str = "", brand: str = "", aliases: Sequence[str] = (),
                     source_ids: Optional[Mapping[str, Any]] = None,
                     extra: Optional[Mapping[str, Any]] = None,
                     sources: Optional[Mapping[Any, Mapping[str, Any]]] = None,
                     assume_accepted: bool = False) -> "ModelSpec":
        """From spec_values rows (the shape spec_sources.to_db_rows() returns,
        or the site table: field, value_num, value_text, unit, status,
        source_url or source_id). Only rows whose status is ``accepted``
        count; a row WITHOUT a status is ignored (logged) unless the caller's
        query already filtered on status and says so with
        ``assume_accepted=True``. Two accepted rows for one field disagree, so
        the field is dropped (missing). Rows sourced from Amazon are ignored.
        ``sources`` maps source_id -> {url, kind, publisher, content_sha256}
        for rows that carry only an id."""
        ids = dict(source_ids or {})
        srcs = dict(sources or {})
        seen: dict[str, list[FieldValue]] = {}
        no_status = amazon = 0
        for row in rows:
            raw_status = row.get("status")
            if raw_status is None or str(raw_status).strip() == "":
                if not assume_accepted:
                    no_status += 1
                    continue
                raw_status = ss.STATUS_ACCEPTED
            if str(raw_status).strip() != ss.STATUS_ACCEPTED:
                continue
            fname = str(row.get("field") or "")
            if not fname:
                continue
            num, text = row.get("value_num"), row.get("value_text")
            if text in ("true", "false") and (num is None or num in (0, 1, 0.0, 1.0)):
                value: Any = text == "true"
            elif num is not None and text in (None, ""):
                value = float(num)
            else:
                value = text
            sid = row.get("source_id")
            if sid is None:
                sid = ids.get(str(row.get("source_url") or ""))
            meta = srcs.get(sid) or {}
            url = str(row.get("source_url") or meta.get("url") or "")
            if url and ss.is_amazon_url(url):
                amazon += 1
                continue
            seen.setdefault(fname, []).append(FieldValue(
                value=value, unit=str(row.get("unit") or ""),
                source_url=url,
                source_kind=str(row.get("source_kind") or meta.get("kind") or ""),
                publisher=str(row.get("publisher") or meta.get("publisher") or ""),
                content_sha256=str(row.get("content_sha256") or meta.get("content_sha256") or ""),
                source_id=sid))
        if no_status:
            log.warning("compat_graph: model %s: %d spec_values rows without a status ignored "
                        "(select the status column, or pass assume_accepted=True)", model_id, no_status)
        if amazon:
            log.warning("compat_graph: model %s: %d spec_values rows sourced from Amazon ignored",
                        model_id, amazon)
        out: dict[str, FieldValue] = {}
        for fname, fvs in seen.items():
            vals = {json.dumps(fv.value, sort_keys=True, default=str) for fv in fvs}
            if len(vals) == 1:
                out[fname] = fvs[0]
            else:
                log.info("compat_graph: model %s field %s has %d accepted values; dropped",
                         model_id, fname, len(vals))
        for k, v in (extra or {}).items():
            out[str(k)] = FieldValue.coerce(v)
        return cls(model_id=model_id, kind=str(kind), values=out, name=name, brand=brand,
                   aliases=tuple(aliases))

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "ModelSpec":
        return cls(model_id=d.get("model_id"), kind=str(d.get("kind") or ""),
                   values={str(k): FieldValue.coerce(v) for k, v in (d.get("values") or {}).items()},
                   name=str(d.get("name") or ""), brand=str(d.get("brand") or ""),
                   aliases=tuple(d.get("aliases") or ()))

    def to_dict(self) -> dict:
        return {"model_id": self.model_id, "kind": self.kind, "name": self.name,
                "brand": self.brand, "aliases": list(self.aliases),
                "values": {k: v.to_dict() for k, v in self.values.items()}}


@dataclass(frozen=True)
class FitRecord:
    """A published or operator-entered fit statement for one pair of models.

    ``a``/``b`` follow the ruleset's orientation (a record given the other way
    round is matched too). ``config`` may name series, parallel, max_units,
    max_series or max_parallel; ``via`` names the exact cable/adapter model."""
    a: Any
    b: Any
    relation: str
    verdict: str = FITS
    origin: str = ORIGIN_MANUFACTURER
    config: Mapping[str, Any] = dc_field(default_factory=dict)
    via: Any = None
    source_url: str = ""
    source_kind: str = ""
    publisher: str = ""
    fetched_at: str = ""
    content_sha256: str = ""
    source_id: Any = None
    statement: str = ""

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "FitRecord":
        return cls(a=d.get("a"), b=d.get("b"), relation=str(d.get("relation") or ""),
                   verdict=str(d.get("verdict") or FITS),
                   origin=str(d.get("origin") or ORIGIN_MANUFACTURER),
                   config=dict(d.get("config") or {}), via=d.get("via"),
                   source_url=str(d.get("source_url") or ""),
                   source_kind=str(d.get("source_kind") or ""),
                   publisher=str(d.get("publisher") or ""),
                   fetched_at=str(d.get("fetched_at") or ""),
                   content_sha256=str(d.get("content_sha256") or ""),
                   source_id=d.get("source_id"), statement=str(d.get("statement") or ""))

    def to_dict(self) -> dict:
        d = {k: getattr(self, k) for k in self.__dataclass_fields__}
        d["config"] = dict(self.config)
        return d

    def signature(self) -> dict:
        return {"a": _k(self.a), "b": _k(self.b), "relation": self.relation,
                "verdict": self.verdict, "origin": self.origin, "config": dict(self.config),
                "via": _k(self.via), "url": self.source_url, "sha": self.content_sha256,
                "source_id": _k(self.source_id), "statement": self.statement}


_RECORD_CONFIG_KEYS = ("series", "parallel", "max_units", "max_series", "max_parallel")


# ---------------------------------------------------------------------------
# Connector taxonomy
# ---------------------------------------------------------------------------

class ConnectorTaxonomy:
    """Connector ids with aliases (regexes that read extracted text), mating
    pairs, ratings and whether panels with that output can be wired in series
    or parallel without conversion.

    ``aliases`` name a connector specifically ("MC4", "DC8020").
    ``fallback_aliases`` are generic wording ("PV connector") that a caller
    may allow on one side only; they count only when nothing specific matched
    in the same piece of text, so "XT60 solar connector" is XT60, never MC4.
    Text is normalised (Unicode hyphens and dashes read as "-", so aliases
    are written with "-") and split into pieces at list separators; a piece
    that names more than one connector, or matches an ``ambiguous_patterns``
    entry ("requires an MC4 adapter"), resolves to nothing: guessing a port
    is how a buyer is told a panel plugs in directly when it needs an
    adapter."""

    def __init__(self, entries: Optional[Mapping[str, Mapping[str, Any]]] = None, *,
                 ambiguous_patterns: Optional[Sequence[str]] = None):
        self.entries: dict[str, dict[str, Any]] = {}
        self.problems: list[str] = []
        self._aliases: list[tuple[str, re.Pattern]] = []
        self._fallback: list[tuple[str, re.Pattern]] = []
        self._by_alnum: dict[str, str] = {}
        for raw_id, e in (entries or {}).items():
            cid = _norm_id(raw_id)
            if not cid:
                continue
            e = dict(e or {})
            ent = {"id": cid, "label": str(e.get("label") or raw_id),
                   "family": str(e.get("family") or ""),
                   "max_a": _as_float(e.get("max_a")), "max_v": _as_float(e.get("max_v")),
                   "series": bool(e.get("series", False)), "parallel": bool(e.get("parallel", False)),
                   "mates_with": tuple(_norm_id(x) for x in _as_list(e.get("mates_with")))}
            self.entries[cid] = ent
            self._by_alnum[_alnum(cid)] = cid
            for key, what, bucket in (("aliases", "alias", self._aliases),
                                      ("fallback_aliases", "fallback alias", self._fallback)):
                for pat in _as_list(e.get(key)):
                    try:
                        bucket.append((cid, re.compile(str(pat), re.I)))
                    except re.error as ex:
                        self.problems.append(f"connector {cid!r}: bad {what} regex {pat!r}: {ex}")
        for cid, ent in self.entries.items():
            for m in ent["mates_with"]:
                if m not in self.entries:
                    self.problems.append(f"connector {cid!r}: mates_with unknown connector {m!r}")
        pats = DEFAULTS["connector_text"]["ambiguous_patterns"] if ambiguous_patterns is None \
            else ambiguous_patterns
        self._ambiguous: list[re.Pattern] = []
        for pat in _as_list(pats):
            try:
                self._ambiguous.append(re.compile(str(pat), re.I))
            except re.error as ex:
                self.problems.append(f"connector_text.ambiguous_patterns: bad regex {pat!r}: {ex}")

    def __contains__(self, cid: object) -> bool:
        return isinstance(cid, str) and _norm_id(cid) in self.entries

    def entry(self, cid: str) -> Optional[dict]:
        return self.entries.get(_norm_id(cid))

    @staticmethod
    def _pieces(value: Any) -> list[str]:
        out: list[str] = []
        for item in _as_list(value):
            if item is None:
                continue
            text = _fold_dashes(ss.normalize_text(str(item)))
            out += [p.strip() for p in re.split(r"\s*(?:[,;|/+&]|\band\b|\bor\b)\s*", text)
                    if p and p.strip()]
        return out

    def _hits(self, piece: str, *, fallback: bool) -> list[str]:
        """Connector ids one piece names, in order of appearance."""
        exact = self._by_alnum.get(_alnum(piece))
        if exact:
            return [exact]
        found = sorted((m.start(), cid) for cid, rx in self._aliases for m in [rx.search(piece)] if m)
        if not found and fallback:
            found = sorted((m.start(), cid) for cid, rx in self._fallback
                           for m in [rx.search(piece)] if m)
        return list(dict.fromkeys(cid for _pos, cid in found))

    def resolve(self, value: Any, *, fallback: bool = False) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """(connector ids, unresolved texts) for a value that may be an id, a
        list, or free text such as "DC8020 / Anderson Powerpole". Each piece
        must name exactly one connector (``fallback`` allows the generic
        fallback aliases); ambiguous pieces are returned as unresolved."""
        ids: list[str] = []
        unresolved: list[str] = []
        for piece in self._pieces(value):
            if any(rx.search(piece) for rx in self._ambiguous):
                unresolved.append(piece)
                continue
            hits = self._hits(piece, fallback=fallback)
            if len(hits) != 1:
                unresolved.append(piece)
                continue
            if hits[0] not in ids:
                ids.append(hits[0])
        return tuple(ids), tuple(unresolved)

    def mentions(self, value: Any) -> frozenset[str]:
        """Every connector id any alias or fallback alias finds anywhere in
        the value, ambiguous pieces included: what a policy exclusion scans
        (fail-closed), never what a connection path is built from."""
        out: set[str] = set()
        for piece in self._pieces(value):
            exact = self._by_alnum.get(_alnum(piece))
            if exact:
                out.add(exact)
            out.update(cid for cid, rx in self._aliases if rx.search(piece))
            out.update(cid for cid, rx in self._fallback if rx.search(piece))
        return frozenset(out)

    def mates(self, x: str, y: str) -> bool:
        if x == y:
            return True
        ex, ey = self.entries.get(x), self.entries.get(y)
        return bool((ex and y in ex["mates_with"]) or (ey and x in ey["mates_with"]))

    def rating(self, cid: str) -> tuple[Optional[float], Optional[float]]:
        e = self.entries.get(cid) or {}
        return e.get("max_a"), e.get("max_v")

    def signature(self, ids: Iterable[str]) -> dict:
        return {i: self.entries.get(i) for i in sorted(set(ids))}


# ---------------------------------------------------------------------------
# Compiled rules and rulesets
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Rule:
    id: str
    kind: str
    version: str
    a_fields: Mapping[str, str]
    b_fields: Mapping[str, str]
    params: Mapping[str, Any]
    description: str = ""

    @functools.cached_property
    def signature(self) -> str:
        """Hash of the rule's kind, version, field maps and effective params."""
        return stable_hash({"kind": self.kind, "version": self.version, "a": dict(self.a_fields),
                            "b": dict(self.b_fields), "params": dict(self.params)},
                           float_precision=9, digest_chars=12)


@dataclass(frozen=True)
class RuleSet:
    id: str
    version: str
    relation: str
    a_kinds: tuple[str, ...]
    b_kinds: tuple[str, ...]
    rules: tuple[Rule, ...]
    arrays: Mapping[str, int]
    pairs: str
    store_verdicts: frozenset[str]
    composite_version: str
    enabled: bool = True
    problems: tuple[str, ...] = ()
    description: str = ""

    def rule_of(self, kind: str) -> Optional[Rule]:
        for r in self.rules:
            if r.kind == kind:
                return r
        return None

    @property
    def computed_rules(self) -> tuple[Rule, ...]:
        return tuple(sorted((r for r in self.rules if r.kind in _COMPUTED_KINDS),
                            key=lambda r: _KIND_ORDER.get(r.kind, 9)))

    @property
    def has_manufacturer_list(self) -> bool:
        return any(r.kind == KIND_MANUFACTURER_LIST for r in self.rules)

    @property
    def rule_versions(self) -> dict[str, str]:
        return {r.id: r.version for r in self.rules}


def composite_version(rs_id: str, rs_version: str, rules: Sequence[Rule]) -> str:
    """'<ruleset>@<v>[<rule>@<v>,...]e<engine>': changes when any member
    rule's version, the ruleset's version or ENGINE_VERSION changes."""
    inner = ",".join(f"{r.id}@{r.version}" for r in rules)
    return f"{rs_id}@{rs_version}[{inner}]e{ENGINE_VERSION}"


# ---------------------------------------------------------------------------
# Outputs: checks, assumptions, options, evidence, edges
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Check:
    rule_id: str
    kind: str
    outcome: str        # pass | fail | unknown | info | skipped
    code: str
    detail: str = ""
    values: Mapping[str, Any] = dc_field(default_factory=dict)

    @property
    def text(self) -> str:
        """Human-readable detail: the arithmetic for numeric checks (rendered
        on demand: checks run for every configuration, few are shown)."""
        return self.detail or _describe(self)

    def to_dict(self, *, compact: bool = False) -> dict:
        """``compact`` (the stored row) drops ``kind`` and ``values``: the
        numbers that matter are at the top of the edge config and ``detail``
        keeps the arithmetic."""
        if compact:
            return {"rule_id": self.rule_id, "outcome": self.outcome, "code": self.code,
                    "detail": self.text}
        vals = {k: (_r(v) if isinstance(v, float) else v) for k, v in self.values.items()}
        return {"rule_id": self.rule_id, "kind": self.kind, "outcome": self.outcome,
                "code": self.code, "detail": self.text, "values": vals}


@dataclass(frozen=True)
class Assumption:
    code: str
    text: str
    params: Mapping[str, Any] = dc_field(default_factory=dict)

    def to_dict(self) -> dict:
        params = {k: (_r(v) if isinstance(v, float) else v) for k, v in self.params.items()}
        return {"code": self.code, "text": self.text, "params": params}


@dataclass(frozen=True)
class ArrayOption:
    """One configuration of identical B units: ``series`` per string,
    ``parallel`` strings."""
    series: int
    parallel: int
    outcome: str        # fits | over_limit | no_fit | unknown
    codes: tuple[str, ...] = ()     # codes of the checks that failed or were unknown
    cold_voc_v: Optional[float] = None
    design_current_a: Optional[float] = None
    array_w: Optional[float] = None
    usable_w: Optional[float] = None

    @property
    def units(self) -> int:
        return self.series * self.parallel

    def to_dict(self, *, compact: bool = False) -> dict:
        d: dict[str, Any] = {"series": self.series, "parallel": self.parallel,
                             "outcome": self.outcome}
        keys = ("cold_voc_v", "usable_w") if compact else (
            "cold_voc_v", "design_current_a", "array_w", "usable_w")
        for k in keys:
            v = getattr(self, k)
            if v is not None:
                d[k] = _r(v, 3)
        if self.codes:
            d["codes"] = list(self.codes)
        return d


@dataclass(frozen=True)
class Evidence:
    """A source an edge rests on: a spec value of one side, or a fit record."""
    role: str           # a | b | via | record
    model_id: Any
    field: str
    source_url: str = ""
    source_kind: str = ""
    source_id: Any = None
    content_sha256: str = ""

    def to_dict(self) -> dict:
        d: dict[str, Any] = {"role": self.role, "field": self.field}
        if self.source_id is not None:
            d["source_id"] = self.source_id
        if self.source_url:
            d["url"] = self.source_url
        if self.source_kind:
            d["kind"] = self.source_kind
        return d


@dataclass(frozen=True)
class Edge:
    a_model_id: Any
    b_model_id: Any
    relation: str
    rule_id: str
    rule_version: str
    verdict: str
    confidence: str
    decided_by: str
    via_model_id: Any = None
    config: Mapping[str, Any] = dc_field(default_factory=dict)
    assumptions: tuple[Assumption, ...] = ()
    checks: tuple[Check, ...] = ()
    options: tuple[ArrayOption, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    flags: tuple[str, ...] = ()
    inputs_hash: str = ""
    computed_at: str = ""
    store: bool = True

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (_k(self.a_model_id), _k(self.b_model_id), self.relation, self.rule_id)

    @property
    def fits(self) -> bool:
        return self.verdict in FIT_VERDICTS

    @property
    def evidence_source_ids(self) -> list:
        ids = {_k(e.source_id): e.source_id for e in self.evidence if e.source_id is not None}
        return [ids[k] for k in sorted(ids, key=lambda s: (len(s), s))]

    def grouped_evidence(self) -> list[dict]:
        """Evidence grouped by source: one entry per document with the
        fields it supplied ('a:pv_voc_max', 'b:voc', 'via:connector_a', ...)."""
        groups: dict[tuple[str, str, str], dict] = {}
        for e in self.evidence:
            key = (_k(e.source_id), e.source_url, e.source_kind)
            g = groups.get(key)
            if g is None:
                g = {}
                if e.source_id is not None:
                    g["source_id"] = e.source_id
                if e.source_url:
                    g["url"] = e.source_url
                if e.source_kind:
                    g["kind"] = e.source_kind
                g["fields"] = []
                groups[key] = g
            tag = f"{e.role}:{e.field}"
            if tag not in g["fields"]:
                g["fields"].append(tag)
        return list(groups.values())

    def row_config(self, *, compact: bool = True) -> dict:
        """The config jsonb: the design's series / parallel / cold_voc_v /
        design_temp_c / max_units, plus assumptions, checks of the shown
        configuration, every evaluated option, flags, evidence and the
        inputs hash ``plan`` compares."""
        cfg = dict(self.config)
        cfg["assumptions"] = [a.to_dict() for a in self.assumptions]
        cfg["checks"] = [c.to_dict(compact=compact) for c in self.checks]
        if self.options:
            cfg["options"] = [o.to_dict(compact=compact) for o in self.options]
        cfg["decided_by"] = self.decided_by
        cfg["flags"] = list(self.flags)
        cfg["evidence"] = (self.grouped_evidence() if compact
                           else [e.to_dict() for e in self.evidence])
        cfg["inputs_hash"] = self.inputs_hash
        return cfg

    def to_row(self) -> dict:
        """The compat_edges row (see :func:`reference_ddl`)."""
        return {"a_model_id": self.a_model_id, "b_model_id": self.b_model_id,
                "relation": self.relation, "verdict": self.verdict, "config": self.row_config(),
                "via_model_id": self.via_model_id, "rule_id": self.rule_id,
                "rule_version": self.rule_version,
                "evidence_source_ids": self.evidence_source_ids,
                "confidence": self.confidence, "computed_at": self.computed_at}

    def to_dict(self) -> dict:
        """Full detail (CLI / debugging): check values, every option field,
        one evidence entry per field."""
        d = self.to_row()
        d["config"] = self.row_config(compact=False)
        d["store"] = self.store
        return d


@dataclass(frozen=True)
class Skip:
    """A pair that produced no edge on purpose (exclusion, disabled ruleset)."""
    a_model_id: Any
    b_model_id: Any
    relation: str
    rule_id: str
    reason: str

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (_k(self.a_model_id), _k(self.b_model_id), self.relation, self.rule_id)

    def to_dict(self) -> dict:
        return {"a_model_id": self.a_model_id, "b_model_id": self.b_model_id,
                "relation": self.relation, "rule_id": self.rule_id, "reason": self.reason}


Evaluation = Union[Edge, Skip]


@dataclass(frozen=True)
class Climate:
    design_temp_c: float
    basis: str = "default"      # default | zip | zip3 | caller
    zip: str = ""


@dataclass
class GraphResult:
    edges: list[Edge]
    skipped: list[Skip]
    problems: list[str]
    climate: Climate
    rulesets: tuple[str, ...]
    disabled: tuple[str, ...] = ()
    unresolved: list[dict] = dc_field(default_factory=list)
    # Set when the build covered only part of the graph (build(only_models=...)
    # or an iter_build batch): plan() then deletes only rows in that part.
    only_models: Optional[frozenset[str]] = None
    a_models: Optional[frozenset[str]] = None

    @property
    def partial(self) -> bool:
        return self.only_models is not None or self.a_models is not None

    def covers(self, a_model_id: Any, b_model_id: Any) -> bool:
        """True when the pair lies in the part of the graph this build
        covered (always true for a full build)."""
        a, b = _k(a_model_id), _k(b_model_id)
        if self.only_models is not None and a not in self.only_models and b not in self.only_models:
            return False
        return self.a_models is None or a in self.a_models

    def stored(self) -> list[Edge]:
        return [e for e in self.edges if e.store]

    def by_verdict(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for e in self.edges:
            out[e.verdict] = out.get(e.verdict, 0) + 1
        return dict(sorted(out.items()))

    def metrics(self) -> dict[str, float]:
        """Flat numbers for RunResult.metrics."""
        fits = sum(1 for e in self.edges if e.fits)
        return {
            "compat_pairs_evaluated": float(len(self.edges) + len(self.skipped)),
            "compat_edges_stored": float(len(self.stored())),
            "compat_edges_fit": float(fits),
            "compat_edges_unknown": float(sum(1 for e in self.edges if e.verdict == UNKNOWN)),
            "compat_edges_manufacturer": float(sum(1 for e in self.edges
                                                   if e.confidence == CONF_MANUFACTURER)),
            "compat_edges_disagree": float(sum(1 for e in self.edges
                                               if "computed-check-disagrees" in e.flags)),
            "compat_edges_unchecked": float(sum(1 for e in self.edges
                                                if "computed-check-unknown" in e.flags)),
            "compat_edges_incomplete": float(sum(1 for e in self.edges
                                                 if "computed-check-incomplete" in e.flags)),
            "compat_pairs_excluded": float(len(self.skipped)),
            "compat_config_problems": float(len(self.problems)),
        }

    def summary(self) -> dict:
        return {"rulesets": list(self.rulesets), "disabled": list(self.disabled),
                "partial": self.partial,
                "design_temp_c": self.climate.design_temp_c, "climate_basis": self.climate.basis,
                "edges": len(self.edges), "stored": len(self.stored()),
                "by_verdict": self.by_verdict(), "skipped": len(self.skipped),
                "problems": list(self.problems), "unresolved": len(self.unresolved)}


# ---------------------------------------------------------------------------
# Internal evaluation state
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Path:
    """How A and B connect (or why we cannot say)."""
    outcome: str                 # pass | fail | unknown | skipped
    code: str                    # direct | via-adapter | no-path | no-documented-path |
                                 # missing:<f> | unresolved-connector | excluded-path
    a_ids: tuple[str, ...] = ()
    b_ids: tuple[str, ...] = ()
    via: Optional[ModelSpec] = None
    joint: tuple[str, ...] = ()   # connector ids the current flows through
    endorsed: bool = False
    detail: str = ""
    evidence: tuple[Evidence, ...] = ()
    candidates_sig: tuple = ()
    rating_a: Optional[float] = None
    rating_v: Optional[float] = None


@dataclass(frozen=True)
class _Adapter:
    """An adapter/cable whose two connector values are manufacturer fit data."""
    node: ModelSpec
    ends: tuple[str, str]
    evidence: tuple[Evidence, ...]
    max_a: Optional[float]
    max_v: Optional[float]
    excluded: bool


@dataclass
class _Computed:
    """The computed rules' result for one pair, plus the context needed to
    evaluate a configuration outside the enumeration on demand (a record's
    stated configuration, or a limit being checked)."""
    rs: RuleSet
    a: ModelSpec
    b: ModelSpec
    climate: Climate
    path: Optional[_Path]
    wiring: dict
    cache: dict
    verdict: str = UNKNOWN
    options: list[ArrayOption] = dc_field(default_factory=list)
    extra_options: list[ArrayOption] = dc_field(default_factory=list)
    by_key: dict[tuple[int, int], ArrayOption] = dc_field(default_factory=dict)
    option_checks: dict[tuple[int, int], tuple[Check, ...]] = dc_field(default_factory=dict)
    option_assm: dict[tuple[int, int], list[Assumption]] = dc_field(default_factory=dict)
    fitting: list[ArrayOption] = dc_field(default_factory=list)
    chosen: Optional[ArrayOption] = None
    single: Optional[ArrayOption] = None
    evidence: list[Evidence] = dc_field(default_factory=list)
    flags: list[str] = dc_field(default_factory=list)

    def checks_of(self, o: Optional[ArrayOption]) -> tuple[Check, ...]:
        return self.option_checks.get((o.series, o.parallel), ()) if o is not None else ()

    @property
    def all_options(self) -> list[ArrayOption]:
        return self.options + self.extra_options


@dataclass(frozen=True)
class _Pick:
    """The configuration a fit statement is stored with."""
    shown: Optional[ArrayOption]          # None: nothing the statement covers is safe
    tier: int
    stated: Optional[ArrayOption] = None  # the record's own configuration, when it names one
    vetoed: bool = False                  # the stated / every covered configuration failed safety
    veto: Optional[Check] = None          # the safety check that failed
    base: tuple[int, int] = (1, 1)        # (series, parallel) the statement itself vouches for


# ---------------------------------------------------------------------------
# The graph
# ---------------------------------------------------------------------------

class CompatGraph:
    """Compiled rules + taxonomy. Pure: evaluate pairs, build a graph, plan
    writes against existing edges."""

    def __init__(self, cfg: Optional[Mapping[str, Any]] = None, *,
                 connectors: Optional[Iterable[Mapping[str, Any]]] = None,
                 clock: Optional[Callable[[], str]] = None):
        self.cfg: dict[str, Any] = dict(cfg) if cfg is not None else resolve_config()
        self.problems: list[str] = list(self.cfg.get("config_problems") or [])
        self._clock = clock or _now_iso
        # Per-evaluation caches keyed by id(model); reset by build() and
        # evaluate_pair() (entries also hold the model, so a reused id() of a
        # collected object can never hit).
        self._conn_cache: dict[tuple[int, str], tuple] = {}
        self._excl_cache: dict[int, tuple[ModelSpec, str]] = {}
        self._node_sig_cache: dict[tuple[int, str, str], tuple[ModelSpec, str]] = {}
        self._static_sigs: dict[str, str] = {}
        self._rules_sigs: dict[str, str] = {}
        tax = dict(self.cfg.get("connectors") or {})
        for row in connectors or ():
            row = dict(row)
            cid = row.pop("id", None)
            if cid:
                tax[str(cid)] = merge_config(tax.get(str(cid)) or {}, row)
        self.taxonomy = ConnectorTaxonomy(
            tax, ambiguous_patterns=(self.cfg.get("connector_text") or {}).get("ambiguous_patterns"))
        self.problems += self.taxonomy.problems
        nec_rows = (self.cfg.get("tables") or {}).get("nec_690_7_a")
        self.nec_table = merged_nec_table(nec_rows)
        self.problems += nec_table_problems(nec_rows)
        self.texts: dict[str, str] = {k: str(v) for k, v in (self.cfg.get("texts") or {}).items()}
        self._policy = self._compile_policy(self.cfg.get("policy") or {})
        self.default_climate = self._compile_climate(self.cfg.get("climate") or {})
        self.rules: dict[str, Rule] = {}
        self._rule_problems: dict[str, list[str]] = {}
        for rid, rc in (self.cfg.get("rules") or {}).items():
            self._compile_rule(str(rid), rc)
        self.rulesets: dict[str, RuleSet] = {}
        for rsid, rc in (self.cfg.get("rulesets") or {}).items():
            rs = self._compile_ruleset(str(rsid), rc)
            self.rulesets[rs.id] = rs
            self.problems += [f"ruleset {rs.id!r}: {p}" for p in rs.problems]

    # ── config compilation ────────────────────────────────────────────────

    def _compile_climate(self, c: Mapping[str, Any]) -> Climate:
        t = _as_float(c.get("default_min_c"))
        if t is None:
            t = DEFAULT_DESIGN_TEMP_C
        if t > DEFAULT_DESIGN_TEMP_C + _EPS:
            self.problems.append(f"climate.default_min_c {t:g} is warmer than the "
                                 f"{DEFAULT_DESIGN_TEMP_C:g} °C floor; using {DEFAULT_DESIGN_TEMP_C:g}")
            t = DEFAULT_DESIGN_TEMP_C
        lo, hi = (_as_list(c.get("plausible_c")) + [None, None])[:2]
        lo = _as_float(lo) if lo is not None else -70.0
        hi = _as_float(hi) if hi is not None else 30.0
        self._zip_min: dict[str, float] = {}
        for z, v in (c.get("zip_min_c") or {}).items():
            fv = _as_float(v)
            zk = re.sub(r"\D", "", str(z))
            if fv is None or len(zk) not in (3, 5) or not (lo <= fv <= hi):
                self.problems.append(f"climate.zip_min_c[{z!r}]={v!r} ignored (needs a 3- or 5-digit "
                                     f"ZIP and a value in {lo:g}..{hi:g} °C)")
                continue
            self._zip_min[zk] = fv
        self._zip3_fallback = bool(c.get("zip3_fallback", True))
        return Climate(design_temp_c=t, basis="default")

    def _compile_policy(self, p: Mapping[str, Any]) -> dict:
        excl = {_norm_id(x) for x in _as_list(p.get("exclude_connectors"))}
        for x in excl:
            if x not in self.taxonomy.entries:
                self.problems.append(f"policy.exclude_connectors: {x!r} is not in the connector "
                                     "taxonomy (texts naming it cannot be recognised)")
        models = []
        for i, m in enumerate(_as_list(p.get("exclude_models"))):
            if not isinstance(m, Mapping) or not m.get("field") or m.get("op") not in (
                    "gte", "gt", "lte", "lt", "eq", "ne", "in", "matches", "truthy"):
                self.problems.append(f"policy.exclude_models[{i}] malformed: {m!r}")
                continue
            mm = dict(m)
            if mm["op"] == "matches":
                try:
                    mm["_rx"] = re.compile(str(mm.get("value") or ""), re.I)
                except re.error as e:
                    self.problems.append(f"policy.exclude_models[{i}] bad regex: {e}")
                    continue
            models.append(mm)
        return {"exclude_connectors": frozenset(excl),
                "connector_model_kinds": frozenset(str(k) for k in
                                                   _as_list(p.get("exclude_connector_model_kinds"))),
                "connector_fields": tuple(str(f) for f in _as_list(p.get("connector_fields"))),
                "exclude_models": models}

    def _kind_params(self, kind: str, own: Mapping[str, Any]) -> dict:
        base = dict(((self.cfg.get("kind_defaults") or {}).get(kind)) or {})
        return merge_config(base, own or {})

    def _compile_rule(self, rid: str, rc: Any) -> None:
        probs: list[str] = []
        self._rule_problems[rid] = probs
        if not isinstance(rc, Mapping):
            probs.append("rule config is not an object")
            return
        kind = str(rc.get("kind") or "")
        if kind not in RULE_KINDS:
            probs.append(f"unknown kind {kind!r}")
            return
        version = rc.get("version")
        if version is None or str(version).strip() == "":
            probs.append("missing version")
            return
        a_f = {str(k): str(v) for k, v in (rc.get("a_fields") or {}).items() if v}
        b_f = {str(k): str(v) for k, v in (rc.get("b_fields") or {}).items() if v}
        req_a, req_b, opt_a, opt_b = _KIND_ROLES[kind]
        for role in req_a - set(a_f):
            probs.append(f"a_fields.{role} is required for kind {kind}")
        for role in req_b - set(b_f):
            probs.append(f"b_fields.{role} is required for kind {kind}")
        for role in set(a_f) - req_a - opt_a:
            probs.append(f"a_fields.{role} is not a role of kind {kind}")
        for role in set(b_f) - req_b - opt_b:
            probs.append(f"b_fields.{role} is not a role of kind {kind}")
        if kind == KIND_MAX_CURRENT and not ({"max_a", "current_limited"} & set(a_f)):
            probs.append("a_fields needs max_a and/or current_limited")
        params = self._kind_params(kind, rc.get("params") or {})
        params = self._apply_floors(rid, kind, params, probs)
        if probs:
            return
        self.rules[rid] = Rule(id=rid, kind=kind, version=str(version), a_fields=a_f,
                               b_fields=b_f, params=params,
                               description=str(rc.get("description") or ""))

    def _apply_floors(self, rid: str, kind: str, params: dict, probs: list[str]) -> dict:
        p = dict(params)
        if kind == KIND_MAX_VOLTAGE_COLD:
            h = _as_float(p.get("headroom"))
            if h is None:
                h = MIN_HEADROOM
            if h < MIN_HEADROOM - _EPS:
                self.problems.append(f"rule {rid!r}: headroom {h:g} is below the {MIN_HEADROOM:g} "
                                     f"floor; using {MIN_HEADROOM:g}")
                h = MIN_HEADROOM
            if h >= 0.5:
                probs.append(f"headroom {h:g} must be below 0.5")
            p["headroom"] = h
            if str(p.get("on_missing", "unknown")) != "unknown":
                self.problems.append(f"rule {rid!r}: the cold-voltage check cannot be skipped; "
                                     "on_missing forced to 'unknown'")
            p["on_missing"] = "unknown"
            band = _as_list(p.get("tc_plausible_pct_per_c"))
            lo, hi = (_as_float(band[0]) if band else None), (_as_float(band[1]) if len(band) > 1 else None)
            if lo is None or hi is None or not (lo < hi <= 0):
                probs.append(f"tc_plausible_pct_per_c must be [low, high] with low < high <= 0, got {band!r}")
            else:
                p["tc_plausible_pct_per_c"] = [lo, hi]
            pats = []
            for pat in _as_list(p.get("non_crystalline_patterns")):
                try:
                    pats.append(re.compile(str(pat), re.I))
                except re.error as e:
                    probs.append(f"non_crystalline_patterns: bad regex {pat!r}: {e}")
            p["_non_crystalline"] = tuple(pats)
        elif kind == KIND_CONNECTOR_PATH:
            n = p.get("max_adapters", MAX_ADAPTERS)
            try:
                n = int(n)
            except (TypeError, ValueError):
                n = MAX_ADAPTERS
            if n > MAX_ADAPTERS or n < 0:
                self.problems.append(f"rule {rid!r}: max_adapters {n} outside 0..{MAX_ADAPTERS}; "
                                     f"using {min(max(n, 0), MAX_ADAPTERS)}")
                n = min(max(n, 0), MAX_ADAPTERS)
            p["max_adapters"] = n
            if p.get("no_path") not in ("unknown", "fail"):
                probs.append(f"no_path must be 'unknown' or 'fail', got {p.get('no_path')!r}")
            sides = [str(x) for x in _as_list(p.get("fallback_alias_sides"))]
            if set(sides) - {"a", "b"}:
                probs.append(f"fallback_alias_sides must list 'a' and/or 'b', got {sides!r}")
            p["fallback_alias_sides"] = tuple(sorted(set(sides) & {"a", "b"}))
        elif kind == KIND_MANUFACTURER_LIST:
            if p.get("on_disagreement") not in ("prefer_manufacturer", "most_conservative", "unknown"):
                probs.append(f"on_disagreement {p.get('on_disagreement')!r} is not one of "
                             "prefer_manufacturer | most_conservative | unknown")
        elif kind == KIND_MAX_CURRENT:
            f = _as_float(p.get("isc_factor"))
            if f is None or f < 1.0:
                self.problems.append(f"rule {rid!r}: isc_factor {p.get('isc_factor')!r} below 1.0; using 1.0")
                f = 1.0
            p["isc_factor"] = f
        if kind == KIND_MIN_VOLTAGE:
            f = _as_float(p.get("vmp_factor"))
            if f is None or f <= 0 or f > 1.0:
                probs.append(f"vmp_factor must be in (0, 1], got {p.get('vmp_factor')!r}")
            else:
                p["vmp_factor"] = f
        if kind != KIND_MAX_VOLTAGE_COLD and p.get("on_missing", "unknown") not in ("unknown", "skip"):
            probs.append(f"on_missing must be 'unknown' or 'skip', got {p.get('on_missing')!r}")
        return p

    def _compile_ruleset(self, rsid: str, rc: Any) -> RuleSet:
        probs: list[str] = []
        rc = rc if isinstance(rc, Mapping) else {}
        version = str(rc.get("version") if rc.get("version") is not None else "").strip()
        if not version:
            probs.append("missing version")
        relation = str(rc.get("relation") or "")
        if not relation:
            probs.append("missing relation")
        a_kinds = tuple(str(k) for k in _as_list(rc.get("a_kinds")))
        b_kinds = tuple(str(k) for k in _as_list(rc.get("b_kinds")))
        if not a_kinds or not b_kinds:
            probs.append("a_kinds and b_kinds are required")
        rules: list[Rule] = []
        for rid in _as_list(rc.get("rules")):
            rid = str(rid)
            if rid in self.rules:
                rules.append(self.rules[rid])
            elif rid in self._rule_problems:
                probs.append(f"rule {rid!r} is invalid: {'; '.join(self._rule_problems[rid])}")
            else:
                probs.append(f"unknown rule {rid!r}")
        if not rules and not probs:
            probs.append("no rules")
        kinds = [r.kind for r in rules]
        for k in set(kinds):
            if kinds.count(k) > 1 and k != KIND_MANUFACTURER_LIST:
                probs.append(f"more than one {k} rule")
        arrays: dict[str, int] = {}
        if rc.get("arrays"):
            for key in ("max_series", "max_parallel", "max_units"):
                try:
                    n = int((rc.get("arrays") or {}).get(key, 1))
                except (TypeError, ValueError):
                    n = 0
                if not 1 <= n <= MAX_ARRAY_UNITS:
                    probs.append(f"arrays.{key} must be 1..{MAX_ARRAY_UNITS}")
                arrays[key] = n
        pairs = str(rc.get("pairs") or ("listed" if all(r.kind == KIND_MANUFACTURER_LIST
                                                         for r in rules) else "all"))
        if pairs not in ("all", "listed"):
            probs.append(f"pairs must be 'all' or 'listed', got {pairs!r}")
        store = rc.get("store_verdicts")
        store_set = frozenset(str(v) for v in _as_list(store if store is not None
                                                       else self.cfg.get("store_verdicts")))
        bad = store_set - set(VERDICTS)
        if bad:
            probs.append(f"store_verdicts has unknown verdicts {sorted(bad)}")
        enabled = bool(rc.get("enabled", True)) and not probs
        return RuleSet(id=rsid, version=version, relation=relation, a_kinds=a_kinds,
                       b_kinds=b_kinds, rules=tuple(rules), arrays=arrays, pairs=pairs,
                       store_verdicts=store_set,
                       composite_version=composite_version(rsid, version, rules),
                       enabled=enabled, problems=tuple(probs),
                       description=str(rc.get("description") or ""))

    # ── public helpers ────────────────────────────────────────────────────

    def text(self, code: str, **params: Any) -> Assumption:
        """An assumption rendered from ``texts[code]``."""
        return Assumption(code=code, text=_render(self.texts.get(code, code), params), params=params)

    @staticmethod
    def _pending(code: str, **params: Any) -> Assumption:
        """An unrendered assumption: checks run for every configuration, but
        only the shown configuration's assumptions are rendered."""
        return Assumption(code=code, text="", params=params)

    def climate(self, *, zip_code: str = "", design_temp_c: Optional[float] = None) -> Climate:
        """Design temperature for a location: the per-ZIP extreme minimum from
        config (5 digits, then the 3-digit prefix), else the default (-40 °C).
        ``design_temp_c`` can only make it colder."""
        base = self.default_climate
        t, basis, zp = base.design_temp_c, "default", ""
        z = re.sub(r"\D", "", str(zip_code or ""))
        if len(z) >= 5 and z[:5] in self._zip_min:
            t, basis, zp = self._zip_min[z[:5]], "zip", z[:5]
        elif len(z) >= 3 and self._zip3_fallback and z[:3] in self._zip_min:
            t, basis, zp = self._zip_min[z[:3]], "zip", z[:3]
        if design_temp_c is not None:
            dt = _as_float(design_temp_c)
            if dt is not None and dt < t - _EPS:
                t, basis = dt, "caller"
        return Climate(design_temp_c=t, basis=basis, zip=zp)

    def ruleset_version(self, rsid: str) -> str:
        return self.rulesets[rsid].composite_version

    def is_stale(self, row: Mapping[str, Any]) -> bool:
        """True when an existing edge row was computed by a different version
        of its ruleset (or by a ruleset that no longer exists)."""
        rs = self.rulesets.get(str(row.get("rule_id") or ""))
        return rs is None or str(row.get("rule_version") or "") != rs.composite_version

    def stale(self, rows: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        """Rows to recompute: a known ruleset at another version. Manual rows
        and rows of unknown rulesets are left alone."""
        return [r for r in rows if str(r.get("confidence") or "") != CONF_MANUAL
                and str(r.get("rule_id") or "") in self.rulesets and self.is_stale(r)]

    def pairs_to_recompute(self, rows: Iterable[Mapping[str, Any]]) -> set[tuple[str, str, str]]:
        """(a, b, ruleset) of stale rows: the models whose specs a caller must
        load to bring the graph up to the current rule versions."""
        return {(_k(r.get("a_model_id")), _k(r.get("b_model_id")), str(r.get("rule_id")))
                for r in self.stale(rows)}

    # ── exclusion ─────────────────────────────────────────────────────────

    def excluded_reason(self, m: ModelSpec) -> str:
        """Why policy keeps this model out of every edge ('' = it may enter)."""
        for rule in self._policy["exclude_models"]:
            kinds = _as_list(rule.get("kinds"))
            if kinds and m.kind not in kinds:
                continue
            fv = m.get(str(rule["field"]))
            if fv is None:
                continue
            if _policy_match(rule, fv.value):
                return f"excluded-model:{rule['field']}:{rule['op']}:{_fmt(rule.get('value'))}"
        excl = self._policy["exclude_connectors"]
        if excl and m.kind in self._policy["connector_model_kinds"]:
            for f in self._policy["connector_fields"]:
                fv = m.get(f)
                if fv is None:
                    continue
                hit = sorted(self.taxonomy.mentions(fv.value) & excl)
                if hit:
                    return f"excluded-connector:{hit[0]}"
        return ""

    def _excluded(self, m: ModelSpec) -> str:
        hit = self._excl_cache.get(id(m))
        if hit is None or hit[0] is not m:
            hit = (m, self.excluded_reason(m))
            self._excl_cache[id(m)] = hit
        return hit[1]

    def _reset_caches(self) -> None:
        self._conn_cache = {}
        self._excl_cache = {}
        self._node_sig_cache = {}

    def rules_sig(self, rs: RuleSet) -> str:
        """One hash of every member rule's effective parameters (stored as
        ``config.rules_sig``; a change without a version bump is reported)."""
        hit = self._rules_sigs.get(rs.id)
        if hit is None:
            hit = _rules_sig(rs)
            self._rules_sigs[rs.id] = hit
        return hit

    # ── numbers ───────────────────────────────────────────────────────────

    @staticmethod
    def _number(m: ModelSpec, field_name: Optional[str], unit: str) -> tuple[Optional[float], Optional[FieldValue], str]:
        """(value in ``unit``, the FieldValue, problem code)."""
        if not field_name:
            return None, None, "unmapped"
        fv = m.get(field_name)
        if fv is None:
            return None, None, f"missing:{field_name}"
        v = _as_float(fv.value)
        if v is None:
            return None, fv, f"not-a-number:{field_name}"
        if fv.unit and unit and ss.canonical_unit(fv.unit) != ss.canonical_unit(unit):
            try:
                v = ss.convert(v, fv.unit, unit)
            except ValueError:
                return None, fv, f"unit:{field_name}:{fv.unit}"
        return v, fv, ""

    # ── rule kinds ────────────────────────────────────────────────────────

    def _voltage_factor(self, rule: Rule, b: ModelSpec, voc: float, climate: Climate
                        ) -> tuple[Optional[float], str, list[Assumption], str, list[Evidence]]:
        """(factor, method, assumptions, problem code, evidence)."""
        t = climate.design_temp_c
        lo, hi = rule.params["tc_plausible_pct_per_c"]
        nec = nec_factor(t, self.nec_table)
        ev: list[Evidence] = []
        tc_pct: Optional[float] = None
        tc_fv = b.get(rule.b_fields.get("tc"))
        tc_field = rule.b_fields.get("tc")
        if tc_fv is not None:
            tc_pct = tc_to_pct_per_c(tc_fv.value, tc_fv.unit or "%/degC", voc)
        else:
            tc_fv = b.get(rule.b_fields.get("tc_mv"))
            tc_field = rule.b_fields.get("tc_mv")
            if tc_fv is not None:
                tc_pct = tc_to_pct_per_c(tc_fv.value, tc_fv.unit or "mV/degC", voc)
        if tc_fv is not None:
            ev.append(_evidence("b", b, tc_field or "", tc_fv))
        if tc_pct is not None and lo - _EPS <= tc_pct <= hi + _EPS:
            f = coefficient_factor(tc_pct, t)
            return f, "temperature-coefficient", [self._pending("voc-tc", tc=round(tc_pct, 3),
                                                             factor=round(f, 4))], "", ev
        if tc_fv is not None:
            # A distrusted coefficient may only make the factor LARGER than
            # NEC's; it is never used alone (below the table an implausibly
            # flat one would make colder weather look safer).
            if nec is None:
                return None, "", [], "design-temp-below-nec-table", ev
            cands = [nec]
            if tc_pct is not None and tc_pct < 0:
                cands.append(coefficient_factor(tc_pct, t))
            f = max(cands + [1.0])
            shown = round(tc_pct, 3) if tc_pct is not None else _fmt(tc_fv.value)
            return f, "nec-690.7a(tc-rejected)", [self._pending("voc-tc-rejected", tc=shown,
                                                             factor=round(f, 4))], "", ev
        tech_field = rule.b_fields.get("technology")
        tech = b.get(tech_field)
        if tech is not None:
            ev.append(_evidence("b", b, tech_field or "", tech))
            text = ss.normalize_text(str(tech.value))
            if any(rx.search(text) for rx in rule.params.get("_non_crystalline", ())):
                return None, "", [], "tc-unknown-non-crystalline", ev
        if nec is None:
            return None, "", [], "design-temp-below-nec-table", ev
        out = [self._pending("voc-nec", factor=nec)]
        if tech is None:
            out.append(self._pending("cell-type-assumed"))
        return nec, "nec-690.7a", out, "", ev

    def _check_cold(self, rule: Rule, a: ModelSpec, b: ModelSpec, series: int, climate: Climate,
                    cache: dict) -> tuple[Check, Optional[float], list[Assumption], list[Evidence]]:
        key = ("cold", rule.id)
        if key not in cache:
            max_v, fa, pa = self._number(a, rule.a_fields.get("max_v"), "V")
            voc, fb, pb = self._number(b, rule.b_fields.get("voc"), "V")
            ev = [x for x in (_evidence("a", a, rule.a_fields.get("max_v", ""), fa) if fa else None,
                              _evidence("b", b, rule.b_fields.get("voc", ""), fb) if fb else None) if x]
            if pa or pb:
                cache[key] = ("missing", [p for p in (pa, pb) if p], ev)
            else:
                f, method, assm, prob, ev2 = self._voltage_factor(rule, b, voc, climate)  # type: ignore[arg-type]
                cache[key] = ("ok", (max_v, voc, f, method, assm, prob), ev + ev2)
        state, data, ev = cache[key]
        if state == "missing":
            return (Check(rule.id, rule.kind, UNKNOWN_OUTCOME, ",".join(data),
                          "needs the input's maximum voltage and the open-circuit voltage"),
                    None, [], ev)
        max_v, voc, f, method, assm, prob = data
        if f is None:
            return (Check(rule.id, rule.kind, UNKNOWN_OUTCOME, prob,
                          {"tc-unknown-non-crystalline": "no published temperature coefficient and "
                           "the NEC table covers crystalline silicon only",
                           "design-temp-below-nec-table": f"{_fmt(climate.design_temp_c)} °C is below "
                           "the NEC 690.7(A) table"}.get(prob, prob),
                          {"design_temp_c": climate.design_temp_c}), None, [], ev)
        headroom = float(rule.params["headroom"])
        cold = series * voc * f
        allowed = max_v * (1.0 - headroom)
        vals = {"series": series, "voc_v": voc, "factor": f, "method": method,
                "design_temp_c": climate.design_temp_c, "cold_voc_v": cold, "max_v": max_v,
                "allowed_v": allowed, "headroom": headroom}
        assm = list(assm) + [self._pending("headroom", headroom_pct=round(headroom * 100, 2),
                                       max_v=max_v, allowed_v=round(allowed, 2))]
        if cold <= allowed + _EPS:
            return Check(rule.id, rule.kind, PASS, "cold-voc-ok", "", vals), cold, assm, ev
        if cold <= max_v + _EPS:
            return (Check(rule.id, rule.kind, FAIL, "cold-voc-insufficient-headroom", "", vals),
                    cold, assm, ev)
        return Check(rule.id, rule.kind, FAIL, "cold-voc-exceeds-max", "", vals), cold, assm, ev

    def _check_min_voltage(self, rule: Rule, a: ModelSpec, b: ModelSpec, series: int
                           ) -> tuple[Check, list[Assumption], list[Evidence]]:
        min_v, fa, pa = self._number(a, rule.a_fields.get("min_v"), "V")
        vmp, fb, pb = self._number(b, rule.b_fields.get("vmp"), "V")
        ev = [x for x in (_evidence("a", a, rule.a_fields.get("min_v", ""), fa) if fa else None,
                          _evidence("b", b, rule.b_fields.get("vmp", ""), fb) if fb else None) if x]
        if pa or pb:
            code = ",".join(p for p in (pa, pb) if p)
            return self._missing(rule, code, "minimum input voltage"), self._missing_assm(
                rule, "Minimum input voltage", code), ev
        string_v = series * vmp * float(rule.params.get("vmp_factor", 1.0))  # type: ignore[operator]
        vals = {"series": series, "vmp_v": vmp, "string_vmp_v": string_v, "min_v": min_v}
        if string_v >= min_v - _EPS:  # type: ignore[operator]
            return Check(rule.id, rule.kind, PASS, "vmp-ok", "", vals), [], ev
        return Check(rule.id, rule.kind, FAIL, "vmp-below-min", "", vals), [], ev

    def _check_current(self, rule: Rule, a: ModelSpec, b: ModelSpec, parallel: int
                       ) -> tuple[Check, Optional[float], list[Assumption], list[Evidence]]:
        isc, fb, pb = self._number(b, rule.b_fields.get("isc"), "A")
        factor = float(rule.params.get("isc_factor", 1.0))
        design_a = parallel * isc * factor if isc is not None else None
        lim_field = rule.a_fields.get("current_limited")
        lim_fv = a.get(lim_field)
        limited = _truthy(lim_fv.value) if lim_fv is not None else None
        max_a, fa, pa = self._number(a, rule.a_fields.get("max_a"), "A")
        if pa == "unmapped":
            pa = f"missing:{lim_field or 'current_limited'}"
        ev = [x for x in (_evidence("a", a, lim_field or "", lim_fv) if lim_fv else None,
                          _evidence("a", a, rule.a_fields.get("max_a", ""), fa) if fa else None,
                          _evidence("b", b, rule.b_fields.get("isc", ""), fb) if fb else None) if x]
        vals = {"parallel": parallel, "isc_a": isc, "isc_factor": factor, "design_a": design_a,
                "max_a": max_a, "current_limited": limited}
        if limited is True:
            return (Check(rule.id, rule.kind, PASS, "current-limited",
                          "the input documents current limiting", vals), design_a,
                    [self._pending("current-limited", max_a=max_a)], ev)
        if pa or pb:
            code = ",".join(p for p in (pa, pb) if p)
            return (self._missing(rule, code, "maximum input current"), design_a,
                    self._missing_assm(rule, "Input current", code), ev)
        assm = [self._pending("current-checked", design_a=round(design_a, 2), max_a=max_a,  # type: ignore[arg-type]
                          isc_factor=factor)]
        if design_a <= max_a + _EPS:  # type: ignore[operator]
            return Check(rule.id, rule.kind, PASS, "current-ok", "", vals), design_a, assm, ev
        return Check(rule.id, rule.kind, FAIL, "current-exceeds-max", "", vals), design_a, assm, ev

    def _check_watts(self, rule: Rule, a: ModelSpec, b: ModelSpec, series: int, parallel: int
                     ) -> tuple[Check, Optional[float], Optional[float], list[Assumption], list[Evidence]]:
        rated, fb, pb = self._number(b, rule.b_fields.get("rated_w"), "W")
        max_w, fa, pa = self._number(a, rule.a_fields.get("max_w"), "W")
        ev = [x for x in (_evidence("a", a, rule.a_fields.get("max_w", ""), fa) if fa else None,
                          _evidence("b", b, rule.b_fields.get("rated_w", ""), fb) if fb else None) if x]
        array_w = series * parallel * rated if rated is not None else None
        if array_w is None or max_w is None:
            code = ",".join(p for p in (pa, pb) if p)
            return Check(rule.id, rule.kind, INFO, code or "missing", "", {}), array_w, None, [], ev
        usable = min(array_w, max_w)
        vals = {"array_w": array_w, "max_w": max_w, "usable_w": usable}
        if array_w > max_w + _EPS:
            return (Check(rule.id, rule.kind, INFO, "watts-clipped", "", vals),
                    array_w, usable, [self._pending("watts-clipped", array_w=array_w, max_w=max_w)], ev)
        return Check(rule.id, rule.kind, INFO, "watts-ok", "", vals), array_w, usable, [], ev

    def _missing(self, rule: Rule, code: str, what: str) -> Check:
        if rule.params.get("on_missing") == "skip":
            return Check(rule.id, rule.kind, SKIPPED, code, f"{what} not published; not checked")
        return Check(rule.id, rule.kind, UNKNOWN_OUTCOME, code, f"needs the {what}")

    def _missing_assm(self, rule: Rule, what: str, code: str) -> list[Assumption]:
        """The "not checked" label of a check relaxed by ``on_missing: skip``
        (attached to every configuration). An unknown check is labelled when
        an edge shows it (:meth:`_assumptions_for`)."""
        if rule.params.get("on_missing") == "skip":
            return [self._pending("not-checked", what=what, reason=self._missing_reason(rule, code))]
        return []

    def _missing_reason(self, rule: Rule, code: str) -> str:
        """Readable reason for a problem code such as ``missing:pv_a_max`` or
        ``missing:pv_v_min,missing:vmp``: each value named by its rule role
        (texts ``role.<side>.<role>``, so a profile can say "the station's
        maximum solar input current"), never by a raw field name."""
        roles: dict[str, tuple[str, str]] = {}
        for side, fmap in (("a", rule.a_fields), ("b", rule.b_fields)):
            for role, fname in fmap.items():
                roles.setdefault(fname, (side, role))
        parts: list[str] = []
        for piece in str(code or "").split(","):
            kind, _, rest = piece.partition(":")
            verb = _VALUE_PROBLEMS.get(kind)
            if verb is None:
                if piece:
                    parts.append(piece)
                continue
            side, role = roles.get(rest.split(":")[0], ("", ""))
            noun = self.texts.get(f"role.{side}.{role}") if role else None
            parts.append(f"{noun or 'a required value'} {verb}")
        return "; ".join(dict.fromkeys(parts)) or "a required value is not published"

    def _unknown_reason(self, rs: RuleSet, ch: Check) -> str:
        """Why a non-safety check could not run, for its "not checked"
        assumption: the missing value, or why the connection is unproven."""
        path_key = {"unresolved-connector": "connector-unresolved",
                    "no-documented-path": "connector-no-documented-path"}.get(ch.code)
        if ch.kind == KIND_CONNECTOR_PATH and path_key:
            return self.texts.get(path_key, ch.text)
        rule = next((r for r in rs.rules if r.id == ch.rule_id), None)
        return self._missing_reason(rule, ch.code) if rule is not None else ch.text

    def prepare_adapters(self, rule: Optional[Rule], nodes: Iterable[ModelSpec]) -> list[_Adapter]:
        """Adapters/cables (``adapter_kinds``) whose two connector values are
        manufacturer fit data (``fit_source_kinds``, never Amazon) and resolve
        to exactly one taxonomy id each. Policy-excluded ones are kept but
        marked, so a pair that connects only through them is reported as
        excluded instead of unknown."""
        if rule is None:
            return []
        kinds = {str(k) for k in _as_list(rule.params.get("adapter_kinds"))}
        af = rule.params.get("adapter_fields") or {}
        allowed = {str(k) for k in _as_list(rule.params.get("fit_source_kinds"))}
        excl = self._policy["exclude_connectors"]
        out: list[_Adapter] = []
        for ad in nodes:
            if ad.kind not in kinds:
                continue
            ends: list[str] = []
            ev: list[Evidence] = []
            for role in ("connector_a", "connector_b"):
                fname = str(af.get(role) or role)
                fv = ad.get(fname)
                if fv is None or fv.source_kind not in allowed or ss.is_amazon_url(fv.source_url):
                    break
                ids, _ = self.taxonomy.resolve(fv.value)
                if len(ids) != 1:
                    break
                ends.append(ids[0])
                ev.append(_evidence("via", ad, fname, fv))
            if len(ends) != 2:
                continue
            ad_a, _, _ = self._number(ad, str(af.get("max_a") or "max_a"), "A")
            ad_v, _, _ = self._number(ad, str(af.get("max_v") or "max_v"), "V")
            excluded = bool(self._excluded(ad)) or ends[0] in excl or ends[1] in excl
            out.append(_Adapter(ad, (ends[0], ends[1]), tuple(ev), ad_a, ad_v, excluded))
        out.sort(key=lambda x: _k(x.node.model_id))
        return out

    def _resolved(self, m: ModelSpec, field_name: str, fallback: bool = False
                  ) -> tuple[Optional[FieldValue], tuple[str, ...]]:
        key = (id(m), field_name, fallback)
        hit = self._conn_cache.get(key)
        if hit is None or hit[0] is not m:
            fv = m.get(field_name)
            ids = self.taxonomy.resolve(fv.value, fallback=fallback)[0] if fv is not None else ()
            hit = (m, fv, ids)
            self._conn_cache[key] = hit
        return hit[1], hit[2]

    def _joint(self, ends: tuple[str, str], a_ids: Sequence[str], b_ids: Sequence[str]
               ) -> Optional[tuple[str, str]]:
        c1, c2 = ends
        for x in a_ids:
            for y in b_ids:
                if (self.taxonomy.mates(c1, x) and self.taxonomy.mates(c2, y)) or \
                        (self.taxonomy.mates(c2, x) and self.taxonomy.mates(c1, y)):
                    return (x, y)
        return None

    def _path(self, rule: Rule, a: ModelSpec, b: ModelSpec, adapters: Sequence[_Adapter],
              endorsed: set[tuple[str, str]]) -> _Path:
        excl = self._policy["exclude_connectors"]
        fa_name, fb_name = rule.a_fields["connectors"], rule.b_fields["connectors"]
        sides = rule.params.get("fallback_alias_sides") or ()
        fa, a_ids = self._resolved(a, fa_name, "a" in sides)
        fb, b_ids = self._resolved(b, fb_name, "b" in sides)
        ev = [x for x in (_evidence("a", a, fa_name, fa) if fa else None,
                          _evidence("b", b, fb_name, fb) if fb else None) if x]
        if fa is None or fb is None:
            # The side that is known still counts: B's own connector decides
            # which wirings exist and its rating is still checked.
            missing = ",".join(f"missing:{n}" for n, v in ((fa_name, fa), (fb_name, fb)) if v is None)
            return _Path(UNKNOWN_OUTCOME, missing, a_ids, b_ids, detail="connector not published",
                         evidence=tuple(ev))
        if not a_ids or not b_ids:
            return _Path(UNKNOWN_OUTCOME, "unresolved-connector", a_ids, b_ids,
                         detail="connector text not in the taxonomy", evidence=tuple(ev))
        a_ok = [x for x in a_ids if x not in excl]
        b_ok = [y for y in b_ids if y not in excl]
        for x in a_ok:
            for y in b_ok:
                if self.taxonomy.mates(x, y):
                    ra = _min_opt(self.taxonomy.rating(x)[0], self.taxonomy.rating(y)[0])
                    rv = _min_opt(self.taxonomy.rating(x)[1], self.taxonomy.rating(y)[1])
                    return _Path(PASS, "direct", a_ids, b_ids, None, (x, y), detail=f"{x} to {y}",
                                 evidence=tuple(ev), rating_a=ra, rating_v=rv)
        use_adapters = int(rule.params.get("max_adapters", MAX_ADAPTERS)) >= 1
        cands: list[tuple[tuple, _Adapter, tuple[str, str], Optional[float], Optional[float]]] = []
        sig: list = []
        if use_adapters and a_ok and b_ok:
            for ad in adapters:
                if ad.excluded:
                    continue
                joint = self._joint(ad.ends, a_ok, b_ok)
                if not joint:
                    continue
                ra = _min_opt(ad.max_a, self.taxonomy.rating(joint[0])[0], self.taxonomy.rating(joint[1])[0])
                rv = _min_opt(ad.max_v, self.taxonomy.rating(joint[0])[1], self.taxonomy.rating(joint[1])[1])
                aid = _k(ad.node.model_id)
                endorsed_hit = (aid, _k(a.model_id)) in endorsed or (aid, _k(b.model_id)) in endorsed
                brand = 2 if ad.node.brand and ad.node.brand.casefold() == (a.brand or "").casefold() else (
                    1 if ad.node.brand and ad.node.brand.casefold() == (b.brand or "").casefold() else 0)
                rank = (0 if endorsed_hit else 1, -brand, -(ra or 0.0), -(rv or 0.0), aid)
                cands.append((rank, ad, joint, ra, rv))
                sig.append({"id": aid, "ends": list(ad.ends), "max_a": ad.max_a, "max_v": ad.max_v,
                            "endorsed": endorsed_hit, "brand": brand})
        if cands:
            rank, ad, joint, ra, rv = min(cands, key=lambda c: c[0])
            return _Path(PASS, "via-adapter", a_ids, b_ids, ad.node, joint, endorsed=rank[0] == 0,
                         detail=f"{joint[1]} to {joint[0]} through {ad.node.label}",
                         evidence=tuple(ev) + ad.evidence, candidates_sig=tuple(sig),
                         rating_a=ra, rating_v=rv)
        if (set(a_ids) & excl or set(b_ids) & excl or any(x.excluded for x in adapters)) and \
                self._connects_through_excluded(a_ids, b_ids, adapters, use_adapters):
            return _Path(SKIPPED, "excluded-path", a_ids, b_ids,
                         detail="connects only through an excluded connector or adapter",
                         evidence=tuple(ev))
        outcome = FAIL if rule.params.get("no_path") == "fail" else UNKNOWN_OUTCOME
        code = "no-path" if outcome == FAIL else "no-documented-path"
        return _Path(outcome, code, a_ids, b_ids, detail="no direct match and no documented adapter",
                     evidence=tuple(ev), candidates_sig=tuple(sig))

    def _connects_through_excluded(self, a_ids: Sequence[str], b_ids: Sequence[str],
                                   adapters: Sequence[_Adapter], use_adapters: bool) -> bool:
        """True when the pair WOULD connect if excluded connectors and
        excluded adapters were allowed."""
        for x in a_ids:
            for y in b_ids:
                if self.taxonomy.mates(x, y):
                    return True
        return use_adapters and any(self._joint(ad.ends, a_ids, b_ids) for ad in adapters)

    def _check_connector(self, rule: Rule, path: _Path, cold_v: Optional[float],
                         design_a: Optional[float], *, need_v: bool = False,
                         need_a: bool = False) -> tuple[Check, list[Assumption]]:
        """The connection path, then its ratings: a known rating must cover
        the configuration's cold voltage and design current. ``need_v`` /
        ``need_a`` say the ruleset computes that quantity; when it does but
        the value is unknown (a missing Voc or Isc), the rating check is
        unknown, never a pass (``on_missing: skip`` labels it instead). On a
        path that did not resolve, B's own known rating still applies: a
        configuration that exceeds it fails."""
        vals: dict[str, Any] = {"path": path.code, "a_connectors": list(path.a_ids),
                                "b_connectors": list(path.b_ids)}
        if path.via is not None:
            vals["via"] = path.via.model_id
        if path.outcome != PASS:
            if rule.params.get("check_ratings", True) and path.b_ids:
                # Whatever the path turns out to be, B's own connector (and a
                # branch connector of its type) carries the string voltage
                # and the array current: a known rating it exceeds fails,
                # so missing A-side data never lifts the safety floor.
                rb_a = _min_opt(*(self.taxonomy.rating(y)[0] for y in path.b_ids))
                rb_v = _min_opt(*(self.taxonomy.rating(y)[1] for y in path.b_ids))
                if rb_v is not None and cold_v is not None and cold_v > rb_v + _EPS:
                    vals.update(rating_v=rb_v, cold_voc_v=cold_v)
                    return Check(rule.id, rule.kind, FAIL, "rating-voltage-exceeded", "", vals), []
                if rb_a is not None and design_a is not None and design_a > rb_a + _EPS:
                    vals.update(rating_a=rb_a, design_a=design_a)
                    return Check(rule.id, rule.kind, FAIL, "rating-current-exceeded", "", vals), []
            if path.outcome == UNKNOWN_OUTCOME and rule.params.get("on_missing") == "skip" \
                    and path.code.startswith("missing:"):
                return (Check(rule.id, rule.kind, SKIPPED, path.code,
                              "connector not published; not checked", vals),
                        [self._pending("not-checked", what=_NOT_CHECKED_WHAT[KIND_CONNECTOR_PATH],
                                       reason=self._missing_reason(rule, path.code))])
            return (Check(rule.id, rule.kind, path.outcome if path.outcome in (FAIL, UNKNOWN_OUTCOME)
                          else UNKNOWN_OUTCOME, path.code, path.detail, vals), [])
        if rule.params.get("check_ratings", True):
            unknown: list[str] = []
            if path.rating_v is not None:
                if cold_v is not None and cold_v > path.rating_v + _EPS:
                    vals.update(rating_v=path.rating_v, cold_voc_v=cold_v)
                    return Check(rule.id, rule.kind, FAIL, "rating-voltage-exceeded", "", vals), []
                if cold_v is None and need_v:
                    unknown.append("rating-voltage-unknown")
            if path.rating_a is not None:
                if design_a is not None and design_a > path.rating_a + _EPS:
                    vals.update(rating_a=path.rating_a, design_a=design_a)
                    return Check(rule.id, rule.kind, FAIL, "rating-current-exceeded", "", vals), []
                if design_a is None and need_a:
                    unknown.append("rating-current-unknown")
            if unknown:
                vals.update(rating_v=path.rating_v, rating_a=path.rating_a)
                code = unknown[0]
                chk = Check(rule.id, rule.kind, UNKNOWN_OUTCOME, code, "", vals)
                if rule.params.get("on_missing") == "skip":
                    return (Check(rule.id, rule.kind, SKIPPED, code, "", vals),
                            [self._pending("rating-not-checked", reason=chk.text)])
                return chk, []
        return Check(rule.id, rule.kind, PASS, path.code, path.detail, vals), []

    # ── one pair ──────────────────────────────────────────────────────────

    def _configs(self, rs: RuleSet, wiring: dict) -> list[tuple[int, int]]:
        if not rs.arrays:
            return [(1, 1)]
        ms, mp, mu = rs.arrays["max_series"], rs.arrays["max_parallel"], rs.arrays["max_units"]
        out = []
        for s in range(1, ms + 1):
            for p in range(1, mp + 1):
                if s * p > mu:
                    continue
                if s > 1 and not wiring["series"]:
                    continue
                if p > 1 and not wiring["parallel"]:
                    continue
                out.append((s, p))
        return out

    def _wiring(self, rs: RuleSet, path: Optional[_Path]) -> dict:
        """Which multi-unit wirings exist. B's own connector decides first
        (taxonomy ``series``/``parallel``). Parallel strings also need a
        branch (Y) connector, an extra part like an adapter: it must fit in
        the connector rule's adapter budget (``max_adapters``, at most one),
        so a path that already goes through an adapter offers no parallel
        strings. Unconstrained without a connector rule or when B's connector
        is unknown."""
        if path is None or not path.b_ids:
            return {"series": True, "parallel": True, "constrained": False}
        ents = [self.taxonomy.entries.get(i) or {} for i in path.b_ids]
        out: dict[str, Any] = {"series": any(e.get("series") for e in ents),
                               "parallel": any(e.get("parallel") for e in ents), "constrained": True}
        conn = rs.rule_of(KIND_CONNECTOR_PATH)
        budget = int((conn.params if conn else {}).get("max_adapters", MAX_ADAPTERS))
        if path.via is not None:
            budget -= 1
        if out["parallel"] and budget < 1:
            out["parallel"] = False
            out["parallel_blocked_by"] = "adapter" if path.via is not None else "max_adapters"
        return out

    def _eval_option(self, rs: RuleSet, a: ModelSpec, b: ModelSpec, path: Optional[_Path],
                     s: int, p: int, climate: Climate, cache: dict
                     ) -> tuple[ArrayOption, tuple[Check, ...], list[Assumption], list[Evidence]]:
        """Every computed rule for one configuration: ``s`` units in series
        per string, ``p`` strings in parallel."""
        checks: list[Check] = []
        assm: list[Assumption] = []
        evidence: list[Evidence] = []
        cold_v = design_a = array_w = usable_w = None
        need_v = rs.rule_of(KIND_MAX_VOLTAGE_COLD) is not None
        need_a = rs.rule_of(KIND_MAX_CURRENT) is not None
        for rule in rs.computed_rules:
            if rule.kind == KIND_MAX_VOLTAGE_COLD:
                chk, cold_v, asm, ev = self._check_cold(rule, a, b, s, climate, cache)
            elif rule.kind == KIND_MIN_VOLTAGE:
                chk, asm, ev = self._check_min_voltage(rule, a, b, s)
            elif rule.kind == KIND_MAX_CURRENT:
                chk, design_a, asm, ev = self._check_current(rule, a, b, p)
            elif rule.kind == KIND_WATTS_CLIP:
                chk, array_w, usable_w, asm, ev = self._check_watts(rule, a, b, s, p)
            else:  # connector_path
                chk, asm = self._check_connector(rule, path, cold_v, design_a,  # type: ignore[arg-type]
                                                 need_v=need_v, need_a=need_a)
                ev = []
            checks.append(chk)
            assm.extend(asm)
            evidence.extend(ev)
        opt = ArrayOption(s, p, _combine(checks),
                          tuple(c.code for c in checks if c.outcome in (FAIL, UNKNOWN_OUTCOME)),
                          cold_v, design_a, array_w, usable_w)
        return opt, tuple(checks), assm, evidence

    def _option_at(self, c: _Computed, s: int, p: int) -> ArrayOption:
        """The option for (s, p): from the enumeration, or evaluated now (a
        configuration a record states, or a limit being checked)."""
        key = (int(s), int(p))
        hit = c.by_key.get(key)
        if hit is None:
            hit, checks, assm, _ev = self._eval_option(c.rs, c.a, c.b, c.path, key[0], key[1],
                                                       c.climate, c.cache)
            c.by_key[key] = hit
            c.option_checks[key] = checks
            c.option_assm[key] = assm
            c.extra_options.append(hit)
        return hit

    def _compute(self, rs: RuleSet, a: ModelSpec, b: ModelSpec, adapters: Sequence[_Adapter],
                 endorsed: set[tuple[str, str]], climate: Climate) -> Union[_Computed, str]:
        conn_rule = rs.rule_of(KIND_CONNECTOR_PATH)
        path = self._path(conn_rule, a, b, adapters, endorsed) if conn_rule else None
        if path is not None and path.code == "excluded-path":
            return "excluded-path"
        c = _Computed(rs=rs, a=a, b=b, climate=climate, path=path, wiring=self._wiring(rs, path),
                      cache={})
        evidence: list[Evidence] = list(path.evidence) if path else []
        for s, p in self._configs(rs, c.wiring):
            opt, checks, assm, ev = self._eval_option(rs, a, b, path, s, p, climate, c.cache)
            c.options.append(opt)
            c.by_key[(s, p)] = opt
            c.option_checks[(s, p)] = checks
            c.option_assm[(s, p)] = assm
            if s == 1 and p == 1:
                evidence.extend(ev)   # every configuration reads the same fields
        c.evidence = _dedupe_evidence(evidence)
        c.fitting = [o for o in c.options if o.outcome == FITS]
        c.single = c.by_key.get((1, 1))
        if c.fitting:
            if c.single is not None and c.single.outcome == FITS:
                verdict = FITS
            elif any(o.parallel == 1 for o in c.fitting):
                verdict = FITS_IN_SERIES
            else:
                verdict = FITS_IN_PARALLEL
            if path is not None and path.via is not None:
                verdict = FITS_WITH_ADAPTER
            c.chosen = min(c.fitting, key=_option_rank)
        else:
            if any(o.outcome == UNKNOWN for o in c.options) or c.single is None:
                verdict = UNKNOWN
            else:
                verdict = c.single.outcome
            c.chosen = c.single
        c.verdict = verdict
        if path is not None and path.via is not None and path.endorsed and verdict in FIT_VERDICTS:
            c.flags.append("adapter-endorsed")
        return c

    def _climate_assumption(self, climate: Climate) -> Assumption:
        if climate.basis == "zip":
            return self.text("design-temp-zip", temp_c=climate.design_temp_c, zip=climate.zip)
        if climate.basis == "caller":
            return self.text("design-temp-caller", temp_c=climate.design_temp_c)
        return self.text("design-temp-default", temp_c=climate.design_temp_c)

    def _assumptions_for(self, c: _Computed, opt: Optional[ArrayOption], verdict: str,
                         via_node: Optional[ModelSpec]) -> list[Assumption]:
        """The assumptions shown next to a verdict, for the configuration the
        edge stores. A cold-voltage check that could not run says so instead
        of naming a design temperature it never applied; every other check
        that could not run is named with the value that is missing."""
        out: list[Assumption] = []
        checks = c.checks_of(opt)
        cold_rule = c.rs.rule_of(KIND_MAX_VOLTAGE_COLD)
        if cold_rule is not None:
            cold = next((ch for ch in checks if ch.kind == KIND_MAX_VOLTAGE_COLD), None)
            if cold is not None and cold.outcome == UNKNOWN_OUTCOME:
                out.append(self.text("cold-not-checked", reason=cold.text))
            else:
                out.append(self._climate_assumption(c.climate))
        if opt is not None:
            shown = c.option_assm.get((opt.series, opt.parallel), [])
            if verdict not in FIT_VERDICTS:
                # Clipping is advice about a working setup; noise on a non-fit.
                shown = [x for x in shown if x.code != "watts-clipped"]
            out.extend(_dedupe_assumptions(self.text(x.code, **x.params) for x in shown))
            for ch in checks:
                if ch.outcome != UNKNOWN_OUTCOME:
                    continue
                if ch.code in _RATING_UNKNOWN_CODES:
                    out.append(self.text("rating-not-checked", reason=ch.text))
                elif ch.kind in _NOT_CHECKED_WHAT:
                    out.append(self.text("not-checked", what=_NOT_CHECKED_WHAT[ch.kind],
                                         reason=self._unknown_reason(c.rs, ch)))
        if c.rs.arrays and cold_rule is not None:
            out.append(self.text("per-input"))
        if opt is not None and opt.parallel > 1 and verdict in FIT_VERDICTS:
            out.append(self.text("parallel-connector", parallel=opt.parallel))
        if via_node is not None and verdict in FIT_VERDICTS:
            out.append(self.text("adapter", via_name=via_node.label, via_id=via_node.model_id))
        return out

    def _config_for(self, c: _Computed, shown: Optional[ArrayOption],
                    acceptable: Sequence[ArrayOption]) -> dict:
        """The config jsonb numbers: the stored configuration (``shown``) and
        its cold-voltage arithmetic, the design temperature, and the limits
        over the ``acceptable`` configurations."""
        rs, path, wiring = c.rs, c.path, c.wiring
        cfg: dict[str, Any] = {}
        if shown is not None:
            cfg["series"] = shown.series
            cfg["parallel"] = shown.parallel
            if shown.cold_voc_v is not None:
                cfg["cold_voc_v"] = _r(shown.cold_voc_v, 3)
            if shown.design_current_a is not None:
                cfg["design_current_a"] = _r(shown.design_current_a, 3)
            if shown.usable_w is not None:
                cfg["usable_w"] = _r(shown.usable_w, 3)
            for ch in c.checks_of(shown):
                if ch.kind == KIND_MAX_VOLTAGE_COLD and ch.values:
                    cfg.update({"voltage_factor": _r(ch.values.get("factor"), 5),
                                "factor_method": ch.values.get("method"),
                                "max_input_v": _r(ch.values.get("max_v"), 3),
                                "allowed_v": _r(ch.values.get("allowed_v"), 3),
                                "headroom": ch.values.get("headroom")})
        if rs.rule_of(KIND_MAX_VOLTAGE_COLD) is not None:
            cfg["design_temp_c"] = c.climate.design_temp_c
            cfg["climate_basis"] = c.climate.basis
        if acceptable:
            cfg["max_units"] = max(o.units for o in acceptable)
            cfg["min_series"] = min(o.series for o in acceptable)
            ser = [o.series for o in acceptable if o.parallel == 1]
            par = [o.parallel for o in acceptable if o.series == 1]
            if ser:
                cfg["max_series"] = max(ser)
            if par:
                cfg["max_parallel"] = max(par)
            if rs.arrays:
                cfg["limits_capped"] = any(o.units == rs.arrays["max_units"] or
                                           o.series == rs.arrays["max_series"] or
                                           o.parallel == rs.arrays["max_parallel"] for o in acceptable)
        if path is not None:
            cfg["connector_path"] = {"code": path.code, "joint": list(path.joint),
                                     "a": list(path.a_ids), "b": list(path.b_ids)}
        if rs.arrays and wiring.get("constrained"):
            cfg["wiring"] = {k: wiring[k] for k in ("series", "parallel", "parallel_blocked_by")
                             if k in wiring}
        return cfg

    # ── safety tiers (record decisions) ──────────────────────────────────

    @staticmethod
    def _safety_failure(checks: Sequence[Check]) -> Optional[Check]:
        """The first failing SAFETY check: cold voltage (with its headroom)
        or a connector/adapter rating. No statement overrides these."""
        for ch in checks:
            if ch.outcome == FAIL and (ch.kind == KIND_MAX_VOLTAGE_COLD or ch.code.startswith("rating-")):
                return ch
        return None

    @staticmethod
    def _safety_unknown(ch: Check) -> bool:
        """A SAFETY check that could not run (cold voltage, a known rating
        whose quantity is unknown)."""
        return ch.outcome == UNKNOWN_OUTCOME and (
            ch.kind == KIND_MAX_VOLTAGE_COLD or ch.code in _RATING_UNKNOWN_CODES)

    @staticmethod
    def _tier(checks: Sequence[Check]) -> int:
        """How well a configuration is supported, best first: 0 every check
        passes; 1 no failure, safety checks pass, another check unknown; 2 no
        failure but a safety check could not run; 3 / 4 a non-safety check
        fails (safety passes / unknown); 5 a safety check fails."""
        if CompatGraph._safety_failure(checks) is not None:
            return 5
        safety_unknown = any(CompatGraph._safety_unknown(ch) for ch in checks)
        if any(ch.outcome == FAIL for ch in checks):
            return 4 if safety_unknown else 3
        if safety_unknown:
            return 2
        return 1 if any(ch.outcome == UNKNOWN_OUTCOME for ch in checks) else 0

    @staticmethod
    def _directions(c: _Computed, ch: Check) -> tuple[bool, bool, bool]:
        """(series_up, parallel_up, series_down): how a check that failed or
        could not run gets worse as a configuration changes. Cold voltage
        and a voltage rating grow with the units in series; the input current
        and a current rating with the parallel strings; the minimum voltage
        is missed with fewer units in series. An unproven connection leaves
        every extra parallel string unverified (it needs a branch connector,
        which an adapter path has no budget for) and, while B's own connector
        is unknown (wiring unconstrained), every extra unit in series too."""
        if ch.kind == KIND_MAX_VOLTAGE_COLD or ch.code in ("rating-voltage-unknown",
                                                           "rating-voltage-exceeded"):
            return True, False, False
        if ch.kind == KIND_MAX_CURRENT or ch.code in ("rating-current-unknown", "rating-current-exceeded"):
            return False, True, False
        if ch.kind == KIND_MIN_VOLTAGE:
            return False, False, True
        if ch.kind == KIND_CONNECTOR_PATH:
            return not c.wiring.get("constrained"), True, False
        return False, False, False

    def _covered(self, c: _Computed, o: ArrayOption, base: tuple[int, int]) -> bool:
        """True when every check that could not run for ``o`` is no worse
        there than at ``base``, the (series, parallel) a statement vouches
        for: the statement covers its own configuration, the computed checks
        have to cover the rest."""
        bs, bp = base
        for ch in c.checks_of(o):
            if ch.outcome != UNKNOWN_OUTCOME:
                continue
            up_s, up_p, down_s = self._directions(c, ch)
            if (up_s and o.series > bs) or (up_p and o.parallel > bp) or (down_s and o.series < bs):
                return False
        return True

    @staticmethod
    def _record_base(stated: str, ints: Mapping[str, int]) -> tuple[int, int]:
        """The (series, parallel) a statement itself vouches for: the
        record's own configuration, else the smallest of its shape (one unit;
        two in series; two parallel strings)."""
        if "series" in ints or "parallel" in ints:
            return ints.get("series", 1), ints.get("parallel", 1)
        if stated == FITS_IN_SERIES:
            return 2, 1
        if stated == FITS_IN_PARALLEL:
            return 1, 2
        return 1, 1

    def evaluate_pair(self, a: ModelSpec, b: ModelSpec, *, ruleset: str,
                      records: Iterable[FitRecord] = (), adapters: Iterable[ModelSpec] = (),
                      climate: Optional[Climate] = None, computed_at: str = "") -> Evaluation:
        """Evaluate one pair under one ruleset. Returns an :class:`Edge` (any
        verdict, including unknown) or a :class:`Skip` (excluded pair or
        disabled ruleset)."""
        rs = self.rulesets.get(ruleset)
        if rs is None:
            raise CompatGraphError(f"unknown ruleset {ruleset!r}")
        self._reset_caches()
        records = list(records)
        recs = [r for r in records if _record_matches(r, rs, a, b)]
        prepared = self.prepare_adapters(rs.rule_of(KIND_CONNECTOR_PATH), adapters)
        problems: list[str] = []
        out = self._evaluate(rs, a, b, recs, prepared, _endorsements(rs, records),
                             climate or self.default_climate, computed_at or self._clock(), problems)
        for p in problems:
            log.warning("compat_graph: %s", p)
        return out

    def _evaluate(self, rs: RuleSet, a: ModelSpec, b: ModelSpec, recs: Sequence[FitRecord],
                  adapters: Sequence[_Adapter], endorsed: set[tuple[str, str]], climate: Climate,
                  computed_at: str, problems: list[str]) -> Evaluation:
        if not rs.enabled:
            return Skip(a.model_id, b.model_id, rs.relation, rs.id, "ruleset-disabled")
        for m in (a, b):
            why = self._excluded(m)
            if why:
                return Skip(a.model_id, b.model_id, rs.relation, rs.id, f"{why}:{_k(m.model_id)}")
        valid: list[FitRecord] = []
        mfr_rule = rs.rule_of(KIND_MANUFACTURER_LIST)
        for r in recs:
            prob = self._record_problem(r, mfr_rule)
            if prob:
                problems.append(f"record {_k(r.a)}->{_k(r.b)} ({rs.relation}): {prob}")
            else:
                valid.append(r)
        computed: Optional[_Computed] = None
        if rs.computed_rules:
            c = self._compute(rs, a, b, adapters, endorsed, climate)
            if isinstance(c, str):
                return Skip(a.model_id, b.model_id, rs.relation, rs.id, c)
            computed = c
        return self._decide(rs, a, b, valid, computed, climate, computed_at)

    def _record_problem(self, r: FitRecord, mfr_rule: Optional[Rule]) -> str:
        if r.verdict not in VERDICTS:
            return f"unknown verdict {r.verdict!r}"
        if r.origin not in RECORD_ORIGINS:
            return f"unknown origin {r.origin!r}"
        if r.origin == ORIGIN_MANUAL:
            return ""
        if mfr_rule is None:
            return "ruleset has no manufacturer_list rule"
        if not (r.source_url or r.source_id is not None):
            return "manufacturer record without a source"
        if r.source_url and ss.is_amazon_url(r.source_url):
            return "Amazon content is never a fit source"
        kinds = {str(k) for k in _as_list(mfr_rule.params.get("source_kinds"))}
        if r.source_kind not in kinds:
            return f"source kind {r.source_kind!r} is not manufacturer fit data"
        return ""

    def _decide(self, rs: RuleSet, a: ModelSpec, b: ModelSpec, recs: Sequence[FitRecord],
                computed: Optional[_Computed], climate: Climate, computed_at: str) -> Edge:
        """Precedence: the latest manual record, then manufacturer records
        (the most conservative statement when they disagree), then the
        computed rules. A fit statement goes through :meth:`_decide_fit`,
        which never stores a configuration that fails a safety check."""
        c = computed
        manual = [r for r in recs if r.origin == ORIGIN_MANUAL]
        mfr = [r for r in recs if r.origin == ORIGIN_MANUFACTURER]
        flags: list[str] = list(c.flags) if c else []
        evidence: list[Evidence] = list(c.evidence) if c else []
        make = functools.partial(self._make_edge, rs, a, b, recs, c, climate, computed_at)
        if not manual and not mfr:
            if c is None:
                return make(UNKNOWN, CONF_COMPUTED, DECIDED_COMPUTED, None, None, {}, [], evidence, flags)
            via_node = c.path.via if c.path is not None and c.verdict in FIT_VERDICTS else None
            return make(c.verdict, CONF_COMPUTED, DECIDED_COMPUTED,
                        via_node.model_id if via_node is not None else None, c.chosen,
                        self._config_for(c, c.chosen, c.fitting),
                        self._assumptions_for(c, c.chosen, c.verdict, via_node), evidence, flags)
        if manual:
            rec = max(manual, key=lambda r: (r.fetched_at, manual.index(r)))
            stated, conf, decided = rec.verdict, CONF_MANUAL, DECIDED_MANUAL
            lead = [self.text("manual", statement=rec.statement or rec.verdict)]
            evidence.append(_record_evidence(rec))
            policy = "prefer_manufacturer"      # the operator's word stands on non-safety checks
        else:
            verdicts = sorted({r.verdict for r in mfr}, key=_CONSERVATIVE_ORDER.index)
            stated = verdicts[0]
            rec = sorted((r for r in mfr if r.verdict == stated),
                         key=lambda r: (r.source_url, _k(r.source_id)))[0]
            conf, decided = CONF_MANUFACTURER, DECIDED_MANUFACTURER
            evidence.extend(_record_evidence(r) for r in mfr)
            lead = [self.text("manufacturer-listed" if stated in FIT_VERDICTS
                              else "manufacturer-not-compatible",
                              publisher=rec.publisher or _host(rec.source_url), source_url=rec.source_url)]
            if len(verdicts) > 1:
                flags.append("records-disagree")
                lead.append(self.text("records-disagree"))
            mfr_rule = rs.rule_of(KIND_MANUFACTURER_LIST)
            policy = str((mfr_rule.params if mfr_rule else {}).get("on_disagreement",
                                                                  "prefer_manufacturer"))
        ints = _record_ints(rec)
        if c is None:
            # A manufacturer_list-only ruleset: the statement is the verdict.
            config: dict[str, Any] = dict(ints)
            if "series" in config or "parallel" in config:
                config.setdefault("series", 1)
                config.setdefault("parallel", 1)
            return make(stated, conf, decided, rec.via, None, config, lead, evidence, flags)
        if stated in FIT_VERDICTS:
            return self._decide_fit(make, c, rec, stated, conf, decided, policy, ints, lead,
                                    evidence, flags)
        # A negative (or unknown) statement stands, and only a via the record
        # itself names survives it.
        shown = (self._option_at(c, ints.get("series", 1), ints.get("parallel", 1))
                 if ("series" in ints or "parallel" in ints) else c.chosen)
        verdict = stated
        assumptions = self._assumptions_for(c, shown, verdict, None) + lead
        if decided == DECIDED_MANUFACTURER and c.verdict in FIT_VERDICTS and stated in FAIL_VERDICTS:
            flags.append("computed-check-disagrees")
            assumptions.append(self.text("computed-disagrees", detail=(
                "the published electrical limits allow it, but the manufacturer's statement is used")))
            if policy == "unknown":
                verdict = UNKNOWN
        config = self._config_for(c, shown, ())
        config.update({k: v for k, v in ints.items() if k.startswith("max_")})
        return make(verdict, conf, decided, rec.via, shown, config, assumptions, evidence, flags)

    def _decide_fit(self, make: Callable[..., Edge], c: _Computed, rec: FitRecord, stated: str,
                    conf: str, decided: str, policy: str, ints: Mapping[str, int],
                    lead: list[Assumption], evidence: list[Evidence], flags: list[str]) -> Edge:
        """A fit statement (manual or manufacturer) against the computed rules.

        The stored configuration is the record's own one, else the
        best-supported configuration the statement covers (:meth:`_tier`). A
        configuration that fails a SAFETY check (cold voltage with its >= 5 %
        headroom, a connector or adapter rating) is never stored as a fit,
        whatever ``on_disagreement`` says: the edge shows the best smaller
        configuration that passes (flag ``record-config-limited``), or
        becomes ``over_limit``. A stated max_series / max_parallel /
        max_units is cut to what passes. Other disagreements (minimum
        voltage, current, connector path) keep the statement under
        ``prefer_manufacturer``; ``most_conservative`` lets the computed
        result stand; ``unknown`` withholds the verdict. Every disagreement
        sets ``computed-check-disagrees``; a safety check that could not run
        sets ``computed-check-unknown``, any other check that could not run
        ``computed-check-incomplete``, and each is named next to the verdict.
        The stored limits (max_series / max_parallel / max_units /
        min_series) count only configurations that pass every check, apart
        from checks that could not run and are no worse there than at the
        configuration the statement vouches for (:meth:`_covered`)."""
        pick = self._record_fit_pick(c, stated, ints)
        limits, clamps = self._clamp_limits(c, ints)
        path_via = c.path.via if (c.path is not None and rec.via is None) else None
        detail = ""
        if pick.vetoed:
            detail = pick.veto.text if pick.veto is not None else ""
        elif pick.shown is not None and pick.tier in (3, 4):
            fail = next((ch for ch in c.checks_of(pick.shown) if ch.outcome == FAIL), None)
            detail = fail.text if fail is not None else ""
        notes = [self.text("record-limit-clamped", stated=_limit_phrase(key, n), allowed=_limit_phrase(key, m),
                           detail=why or "a larger configuration fails a safety check")
                 for key, n, m, why in clamps]
        disagrees = [self.text("computed-disagrees", detail=detail)] if detail else []
        if pick.vetoed or (pick.shown is not None and pick.tier in (3, 4)) or clamps:
            flags.append("computed-check-disagrees")
            if decided == DECIDED_MANUFACTURER and policy == "unknown":
                shown = pick.shown or pick.stated
                return make(UNKNOWN, conf, decided, None, shown, self._config_for(c, shown, ()),
                            self._assumptions_for(c, shown, UNKNOWN, None) + lead + disagrees + notes,
                            evidence, flags)
            if decided == DECIDED_MANUFACTURER and policy == "most_conservative":
                # The computed result stands: its verdict, configuration and path.
                via_node = c.path.via if c.path is not None and c.verdict in FIT_VERDICTS else None
                return make(c.verdict, CONF_COMPUTED, DECIDED_COMPUTED,
                            via_node.model_id if via_node is not None else None, c.chosen,
                            self._config_for(c, c.chosen, c.fitting),
                            self._assumptions_for(c, c.chosen, c.verdict, via_node) + lead
                            + disagrees + notes, evidence, flags)
        if pick.shown is None:
            # Every configuration the statement covers fails a safety check:
            # over the limit, decided by the computed rules (a manual record
            # keeps owning the row, so a later manual fix can replace it).
            shown = pick.stated
            return make(OVER_LIMIT, CONF_MANUAL if decided == DECIDED_MANUAL else CONF_COMPUTED,
                        DECIDED_COMPUTED, None, shown, self._config_for(c, shown, ()),
                        self._assumptions_for(c, shown, OVER_LIMIT, None) + lead + disagrees
                        + [self.text("safety-veto")], evidence, flags)
        shown = pick.shown
        verdict = self._fit_label(c, stated, shown, path_via)
        via = rec.via if rec.via is not None else (path_via.model_id if path_via is not None else None)
        acceptable = [o for o in c.all_options
                      if self._tier(c.checks_of(o)) <= 1 and self._covered(c, o, pick.base)]
        config = self._config_for(c, shown, acceptable)
        config.update(limits)
        assumptions = self._assumptions_for(c, shown, verdict, path_via) + lead
        if pick.vetoed:
            flags.append("record-config-limited")
            assumptions.append(self.text("record-config-limited", stated=_config_phrase(pick.stated),
                                         detail=detail, shown=_config_phrase(shown)))
        else:
            assumptions += disagrees
        if clamps:
            flags.append("record-config-limited")
            assumptions += notes
        if pick.tier in (2, 4):
            flags.append("computed-check-unknown")
        if any(ch.outcome == UNKNOWN_OUTCOME and not self._safety_unknown(ch) for ch in c.checks_of(shown)):
            flags.append("computed-check-incomplete")
        return make(verdict, conf, decided, via, shown, config, assumptions, evidence, flags)

    def _record_fit_pick(self, c: _Computed, stated: str, ints: Mapping[str, int]) -> _Pick:
        """The configuration a fit statement is stored with: the record's
        own (series, parallel) when it names one (evaluated even outside the
        enumeration), else the best-supported enumerated configuration within
        the record's limits and of the statement's shape (fits_in_series:
        two or more in series). Ranked by :meth:`_tier`, then the fewest
        units along the axis of any check that failed or could not run
        (:meth:`_directions`; beyond what the statement names, or at all for
        a safety check: nothing is extrapolated past an unchecked or failing
        value, so a missing input current keeps one string), then the
        computed recommendation order. A stated configuration that fails
        a safety check falls back to the best smaller one of the same shape
        that fails no check and whose unchecked values are no worse than at
        the stated one (fewer units in series can miss an unknown minimum
        voltage)."""
        lim = {k: ints[k] for k in ("max_series", "max_parallel", "max_units") if k in ints}
        base = self._record_base(stated, ints)

        def tier(o: ArrayOption) -> int:
            return self._tier(c.checks_of(o))

        def within(o: ArrayOption) -> bool:
            return (o.series <= lim.get("max_series", o.series)
                    and o.parallel <= lim.get("max_parallel", o.parallel)
                    and o.units <= lim.get("max_units", o.units))

        def shaped(o: ArrayOption) -> bool:
            if stated == FITS_IN_SERIES:
                return o.series >= 2
            if stated == FITS_IN_PARALLEL:
                return o.parallel >= 2
            return True

        def rank(o: ArrayOption) -> tuple:
            # Along the axis of every check that failed or could not run,
            # prefer fewer units: a cold voltage (or voltage rating) grows
            # with the units in series, the input current (or a current
            # rating) with the parallel strings, an unproven connection with
            # both (series only while B's own connector is unknown). A SAFETY
            # check that could not run counts every unit; any other counts
            # only units beyond what the statement names (it vouches for its
            # own configuration). Missing data never buys a bigger array.
            safe_s = safe_p = other_s = other_p = False
            for ch in c.checks_of(o):
                if ch.outcome not in (UNKNOWN_OUTCOME, FAIL):
                    continue
                up_s, up_p, _down = self._directions(c, ch)
                if self._safety_unknown(ch):
                    safe_s, safe_p = safe_s or up_s, safe_p or up_p
                else:
                    other_s, other_p = other_s or up_s, other_p or up_p
            s_pen = o.series if safe_s else (max(0, o.series - base[0]) if other_s else 0)
            p_pen = o.parallel if safe_p else (max(0, o.parallel - base[1]) if other_p else 0)
            return (tier(o), s_pen, p_pen) + _option_rank(o)

        if "series" in ints or "parallel" in ints:
            own = self._option_at(c, ints.get("series", 1), ints.get("parallel", 1))
            t = tier(own)
            if t < 5:
                return _Pick(shown=own, tier=t, stated=own, base=base)
            smaller = [o for o in c.all_options if o is not own and o.series <= own.series
                       and o.parallel <= own.parallel and shaped(o) and within(o) and tier(o) <= 2
                       and self._covered(c, o, base)]
            best = min(smaller, key=rank) if smaller else None
            return _Pick(shown=best, tier=tier(best) if best is not None else 5, stated=own,
                         vetoed=True, veto=self._safety_failure(c.checks_of(own)), base=base)
        cands = [o for o in c.options if within(o)] or list(c.options)
        typed = [o for o in cands if shaped(o)]
        if not typed and stated in (FITS_IN_SERIES, FITS_IN_PARALLEL):
            typed = [self._option_at(c, 2, 1) if stated == FITS_IN_SERIES else self._option_at(c, 1, 2)]
        cands = typed or cands
        best = min(cands, key=rank)
        if tier(best) == 5:
            rep = min(cands, key=lambda o: (o.units, o.series, o.parallel))
            return _Pick(shown=None, tier=5, stated=rep, vetoed=True,
                         veto=self._safety_failure(c.checks_of(rep)), base=base)
        return _Pick(shown=best, tier=tier(best), base=base)

    def _clamp_limits(self, c: _Computed, ints: Mapping[str, int]
                      ) -> tuple[dict[str, int], list[tuple[str, int, int, str]]]:
        """A record's max_series / max_parallel / max_units, each cut to the
        largest value whose configuration passes the safety checks
        (evaluated on demand, up to MAX_ARRAY_UNITS). Returns the limits and
        the cuts as (key, stated, allowed, why)."""
        out: dict[str, int] = {}
        cuts: list[tuple[str, int, int, str]] = []
        memo: dict[tuple[str, int], tuple[int, str]] = {}

        def safe_max(axis: str, n: int) -> tuple[int, str]:
            if (axis, n) not in memo:
                allowed, why = 0, ""
                for k in range(1, min(n, MAX_ARRAY_UNITS) + 1):
                    o = self._option_at(c, k, 1) if axis == "series" else self._option_at(c, 1, k)
                    fail = self._safety_failure(c.checks_of(o))
                    if fail is not None:
                        why = fail.text
                        break
                    allowed = k
                else:
                    if n > MAX_ARRAY_UNITS:
                        why = f"configurations beyond {MAX_ARRAY_UNITS} units are not evaluated"
                memo[(axis, n)] = (allowed, why)
            return memo[(axis, n)]

        for key, axis in (("max_series", "series"), ("max_parallel", "parallel")):
            if key in ints:
                allowed, why = safe_max(axis, ints[key])
                out[key] = allowed
                if allowed < ints[key]:
                    cuts.append((key, ints[key], allowed, why))
        if "max_units" in ints:
            n = ints["max_units"]
            s_max, s_why = (out["max_series"], "") if "max_series" in out else safe_max("series", n)
            p_max, p_why = (out["max_parallel"], "") if "max_parallel" in out else safe_max("parallel", n)
            allowed = min(n, s_max * p_max)
            out["max_units"] = allowed
            if allowed < n:
                cuts.append(("max_units", n, allowed, s_why or p_why))
        return out, cuts

    def _fit_label(self, c: _Computed, stated: str, shown: ArrayOption,
                   path_via: Optional[ModelSpec]) -> str:
        """The verdict label of a stated fit, made consistent with the edge:
        a computed adapter path is ``fits_with_adapter`` (as for computed
        edges); a plain "fits" shown as a series string while one unit alone
        fails a check is ``fits_in_series``."""
        if path_via is not None:
            return FITS_WITH_ADAPTER
        if stated == FITS and shown.series > 1 and any(
                ch.outcome == FAIL for ch in c.checks_of(c.single)):
            return FITS_IN_SERIES
        return stated

    def _make_edge(self, rs: RuleSet, a: ModelSpec, b: ModelSpec, recs: Sequence[FitRecord],
                   c: Optional[_Computed], climate: Climate, computed_at: str, verdict: str,
                   conf: str, decided: str, via: Any, shown: Optional[ArrayOption], config: Mapping,
                   assumptions: Sequence[Assumption], evidence: Sequence[Evidence],
                   flags: Sequence[str]) -> Edge:
        cfg = dict(config)
        cfg["rules_sig"] = self.rules_sig(rs)
        path_via = c.path.via.model_id if (c is not None and c.path is not None
                                           and c.path.via is not None) else None
        if verdict not in FIT_VERDICTS or via is None or _k(via) != _k(path_via):
            # The endorsement is about the computed adapter, on a fit that uses it.
            flags = [f for f in flags if f != "adapter-endorsed"]
        return Edge(a_model_id=a.model_id, b_model_id=b.model_id, relation=rs.relation,
                    rule_id=rs.id, rule_version=rs.composite_version, verdict=verdict,
                    confidence=conf, decided_by=decided, via_model_id=via, config=cfg,
                    assumptions=tuple(_dedupe_assumptions(assumptions)),
                    checks=c.checks_of(shown) if c is not None else (),
                    options=tuple(c.all_options) if c is not None else (),
                    evidence=tuple(_dedupe_evidence(evidence)), flags=tuple(dict.fromkeys(flags)),
                    inputs_hash=self._inputs_signature(rs, a, b, recs, c, climate),
                    computed_at=computed_at, store=verdict in rs.store_verdicts)

    def _static_signature(self, rs: RuleSet) -> str:
        """Everything about a ruleset that is the same for every pair: rule
        versions and effective params, the NEC table, the taxonomy, policy,
        texts and stored verdicts. Hashed once per graph."""
        hit = self._static_sigs.get(rs.id)
        if hit is None:
            cold = rs.rule_of(KIND_MAX_VOLTAGE_COLD) is not None
            hit = stable_hash({
                "rs": rs.composite_version,
                "rules": {r.id: r.signature for r in rs.rules},
                "nec": self.nec_table if cold else None,
                "tax": self.taxonomy.signature(self.taxonomy.entries)
                if rs.rule_of(KIND_CONNECTOR_PATH) else None,
                "policy": {"conn": sorted(self._policy["exclude_connectors"]),
                           "models": [{k: v for k, v in m.items() if k != "_rx"}
                                      for m in self._policy["exclude_models"]]},
                "texts": {k: self.texts.get(k) for k in sorted(self.texts)},
                "store": sorted(rs.store_verdicts),
            }, float_precision=9, digest_chars=16)
            self._static_sigs[rs.id] = hit
        return hit

    def _node_hash(self, m: ModelSpec, rs: RuleSet, side: str) -> str:
        """Hash of the values (and their sources) a ruleset reads from one side."""
        key = (id(m), rs.id, side)
        hit = self._node_sig_cache.get(key)
        if hit is None or hit[0] is not m:
            fields: set[str] = set()
            for r in rs.rules:
                fields.update((r.a_fields if side == "a" else r.b_fields).values())
            if side == "via":
                fields = set(m.values)
            hit = (m, stable_hash(_node_signature(m, fields), float_precision=6, digest_chars=16))
            self._node_sig_cache[key] = hit
        return hit[1]

    def _inputs_signature(self, rs: RuleSet, a: ModelSpec, b: ModelSpec, recs: Sequence[FitRecord],
                          computed: Optional[_Computed], climate: Climate) -> str:
        path = computed.path if computed else None
        payload = {
            "s": self._static_signature(rs),
            "t": climate.design_temp_c if rs.rule_of(KIND_MAX_VOLTAGE_COLD) else None,
            "a": self._node_hash(a, rs, "a"),
            "b": self._node_hash(b, rs, "b"),
            "r": sorted(json.dumps(r.signature(), sort_keys=True, default=str) for r in recs),
            "p": None if path is None else {
                "code": path.code, "via": _k(path.via.model_id) if path.via else "",
                "via_h": self._node_hash(path.via, rs, "via") if path.via else "",
                "joint": list(path.joint), "cands": list(path.candidates_sig)},
        }
        return stable_hash(payload, float_precision=6, digest_chars=16)

    # ── whole graph ───────────────────────────────────────────────────────

    def build(self, models: Iterable[ModelSpec], records: Iterable[FitRecord] = (), *,
              rulesets: Optional[Sequence[str]] = None, climate: Optional[Climate] = None,
              only_models: Optional[Iterable[Any]] = None,
              a_models: Optional[Iterable[Any]] = None) -> GraphResult:
        """Evaluate every applicable pair. Rulesets whose rules are only
        ``manufacturer_list`` (or ``pairs: listed``) evaluate only the pairs
        that have records. ``only_models`` restricts work to pairs touching
        those ids (incremental runs after some models changed); ``a_models``
        to pairs whose A side is one of them (disjoint batches, see
        :meth:`iter_build`). The result records either restriction, so
        :meth:`plan` deletes only rows inside the part that was built. Stored
        edges use ``climate`` (default: the -40 °C default): leave it unset
        for shared tables (:meth:`plan` refuses a warmer one)."""
        self._reset_caches()
        nodes = list(models)
        recs = list(records)
        clim = climate or self.default_climate
        stamp = self._clock()
        problems: list[str] = list(self.problems)
        wanted = list(rulesets) if rulesets is not None else list(self.rulesets)
        for rsid in wanted:
            if rsid not in self.rulesets:
                problems.append(f"unknown ruleset {rsid!r}")
        only = {_k(x) for x in only_models} if only_models is not None else None
        a_only = {_k(x) for x in a_models} if a_models is not None else None
        counts = collections.Counter(_k(n.model_id) for n in nodes)
        dup = sorted(i for i, c in counts.items() if c > 1)
        if dup:
            problems.append(f"duplicate model ids (last one wins): {dup[:10]}")
        by_id = {_k(n.model_id): n for n in nodes}
        edges: list[Edge] = []
        skipped: list[Skip] = []
        disabled: list[str] = []
        unresolved: list[dict] = []
        used: list[str] = []
        for rsid in wanted:
            rs = self.rulesets.get(rsid)
            if rs is None:
                continue
            if not rs.enabled:
                disabled.append(rs.id)
                continue
            used.append(rs.id)
            conn_rule = rs.rule_of(KIND_CONNECTOR_PATH)
            adapters = self.prepare_adapters(conn_rule, by_id.values())
            endorsed = _endorsements(rs, recs)
            rs_recs: dict[frozenset, list[FitRecord]] = {}
            for r in recs:
                if r.relation == rs.relation:
                    rs_recs.setdefault(frozenset((_k(r.a), _k(r.b))), []).append(r)
            a_nodes = [n for n in by_id.values() if n.kind in rs.a_kinds
                       and (a_only is None or _k(n.model_id) in a_only)]
            b_nodes = [n for n in by_id.values() if n.kind in rs.b_kinds]
            for key_recs in rs_recs.values():
                ends = [_k(key_recs[0].a), _k(key_recs[0].b)]
                missing = [k for k in ends if k not in by_id]
                if missing:
                    unresolved.append({"ruleset": rs.id, "record_models": ends, "missing": missing})
            if rs.pairs == "listed":
                pairs = []
                seen_pairs: set[tuple[str, str]] = set()
                for key_recs in rs_recs.values():
                    ends = [_k(key_recs[0].a), _k(key_recs[0].b)]
                    if any(k not in by_id for k in ends):
                        continue
                    for x, y in ((ends[0], ends[1]), (ends[1], ends[0])):
                        na, nb = by_id[x], by_id[y]
                        if x != y and na.kind in rs.a_kinds and nb.kind in rs.b_kinds \
                                and (a_only is None or x in a_only) and (x, y) not in seen_pairs:
                            seen_pairs.add((x, y))
                            pairs.append((na, nb))
            elif only is None:
                pairs = [(x, y) for x in a_nodes for y in b_nodes if x is not y]
            else:   # incremental: only pairs touching the listed models
                pairs = [(x, y) for x in a_nodes if _k(x.model_id) in only
                         for y in b_nodes if x is not y]
                pairs += [(x, y) for y in b_nodes if _k(y.model_id) in only
                          for x in a_nodes if x is not y and _k(x.model_id) not in only]
            if conn_rule is not None:
                fa = conn_rule.a_fields["connectors"]
                fb = conn_rule.b_fields["connectors"]
                sides = conn_rule.params.get("fallback_alias_sides") or ()
                for n, f, fb_ok in ([(x, fa, "a" in sides) for x in a_nodes]
                                    + [(y, fb, "b" in sides) for y in b_nodes]):
                    fv = n.get(f)
                    if fv is not None:
                        _, unres = self.taxonomy.resolve(fv.value, fallback=fb_ok)
                        if unres:
                            unresolved.append({"ruleset": rs.id, "model_id": n.model_id,
                                               "field": f, "texts": list(unres)})
            for na, nb in pairs:
                if only is not None and _k(na.model_id) not in only and _k(nb.model_id) not in only:
                    continue
                pair_recs = [r for r in rs_recs.get(frozenset((_k(na.model_id), _k(nb.model_id))), [])
                             if _record_matches(r, rs, na, nb)]
                out = self._evaluate(rs, na, nb, pair_recs, adapters, endorsed, clim, stamp, problems)
                (edges if isinstance(out, Edge) else skipped).append(out)  # type: ignore[arg-type]
        edges.sort(key=lambda e: (e.rule_id, e.key))
        skipped.sort(key=lambda s: (s.rule_id, s.key))
        return GraphResult(edges=edges, skipped=skipped, problems=list(dict.fromkeys(problems)),
                           climate=clim, rulesets=tuple(used), disabled=tuple(disabled),
                           unresolved=unresolved,
                           only_models=frozenset(only) if only is not None else None,
                           a_models=frozenset(a_only) if a_only is not None else None)

    def iter_build(self, models: Iterable[ModelSpec], records: Iterable[FitRecord] = (), *,
                   batch_size: int = 25, rulesets: Optional[Sequence[str]] = None,
                   climate: Optional[Climate] = None) -> Iterable[tuple[list[Any], GraphResult]]:
        """Build in disjoint batches of A-side models: yields (A model ids,
        GraphResult) so a caller can plan and write each batch (filter the
        existing rows to those A ids) and memory stays per batch. Every pair
        appears in exactly one batch; each result is marked partial, so even
        ``plan(..., scope="full")`` against unfiltered rows deletes only rows
        of that batch's A models."""
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        nodes = list(models)
        recs = list(records)
        wanted = [self.rulesets[r] for r in (rulesets if rulesets is not None else self.rulesets)
                  if r in self.rulesets]
        a_kinds = {k for rs in wanted for k in rs.a_kinds}
        a_ids = list(dict.fromkeys(n.model_id for n in nodes if n.kind in a_kinds))
        for i in range(0, len(a_ids), batch_size):
            batch = a_ids[i:i + batch_size]
            yield batch, self.build(nodes, recs, rulesets=rulesets, climate=climate, a_models=batch)

    # ── write plan ────────────────────────────────────────────────────────

    def plan(self, result: GraphResult, existing: Iterable[Mapping[str, Any]], *,
             scope: str = "evaluated", purge_orphaned: bool = False,
             allow_location_climate: bool = False) -> "EdgePlan":
        """Diff a build against existing compat_edges rows.

        * upsert: new edges, and edges whose rule_version, inputs_hash,
          verdict, via or confidence changed (a version bump rewrites);
        * delete: rows of pairs evaluated in this build that no longer produce
          a stored edge (now unknown, excluded or not stored); with
          ``scope="full"`` also rows of the build's rulesets whose pair was
          not evaluated at all (the model left the catalog). A partial build
          (``only_models`` / an :meth:`iter_build` batch) deletes only rows
          inside the part it covered, whatever the scope;
        * stale: rows at an old rule_version that this build did not cover
          (load those models and rebuild; see :meth:`pairs_to_recompute`);
        * protected: manual rows are never overwritten or deleted;
        * orphaned: rows of rulesets this graph has disabled or does not
          know. Listed only (another config layer may own them); with
          ``purge_orphaned=True`` the non-manual ones are deleted (the caller
          must then load every row: ``EdgeWriter.load_existing()``).

        Shared rows must be computed at the default design temperature: a
        build at a warmer climate (a per-ZIP or caller climate) raises
        :class:`CompatGraphError` unless ``allow_location_climate=True``; a
        colder one is allowed with a warning."""
        if scope not in ("evaluated", "full"):
            raise ValueError("scope must be 'evaluated' or 'full'")
        plan = EdgePlan(disabled=list(result.disabled))
        default_t = self.default_climate.design_temp_c
        warm = [e for e in result.edges if e.store
                and _as_float((e.config or {}).get("design_temp_c")) is not None
                and float(e.config["design_temp_c"]) > default_t + _EPS]
        if result.climate.design_temp_c > default_t + _EPS or warm:
            if not allow_location_climate:
                raise CompatGraphError(
                    f"plan(): edges computed at {_fmt(result.climate.design_temp_c)} °C "
                    f"({result.climate.basis}{' ' + result.climate.zip if result.climate.zip else ''}) "
                    f"are warmer than the {_fmt(default_t)} °C default design temperature; shared "
                    "compat_edges rows are written at the default (pass allow_location_climate=True "
                    "for a per-location table)")
            plan.warnings.append(f"edges computed at a location climate "
                                 f"({_fmt(result.climate.design_temp_c)} °C, {result.climate.basis})")
        elif result.climate.basis != "default":
            plan.warnings.append(f"edges computed at a colder {result.climate.basis} climate "
                                 f"({_fmt(result.climate.design_temp_c)} °C)")
        rows: dict[tuple[str, str, str, str], Mapping[str, Any]] = {}
        for r in existing:
            key = (_k(r.get("a_model_id")), _k(r.get("b_model_id")), str(r.get("relation") or ""),
                   str(r.get("rule_id") or ""))
            if key in rows:
                plan.warnings.append(f"duplicate existing row {key}")
            rows[key] = r
        active = set(result.rulesets)
        desired = {e.key: e for e in result.edges if e.store}
        evaluated = {e.key for e in result.edges} | {s.key for s in result.skipped}
        for key, edge in desired.items():
            row = rows.get(key)
            if row is None:
                plan.upserts.append(edge)
                continue
            if str(row.get("confidence") or "") == CONF_MANUAL and edge.confidence != CONF_MANUAL:
                plan.protected.append(_key_dict(row))
                continue
            cfg = _row_config(row)
            same = (str(row.get("rule_version") or "") == edge.rule_version
                    and str(cfg.get("inputs_hash") or "") == edge.inputs_hash
                    and str(row.get("verdict") or "") == edge.verdict
                    and _k(row.get("via_model_id")) == _k(edge.via_model_id)
                    and str(row.get("confidence") or "") == edge.confidence)
            if same:
                plan.unchanged.append(_key_dict(row))
            else:
                if str(row.get("rule_version") or "") != edge.rule_version:
                    plan.recomputed.append(_key_dict(row))
                else:
                    sig_old = str(cfg.get("rules_sig") or "")
                    if sig_old and sig_old != self.rules_sig(self.rulesets[edge.rule_id]):
                        plan.warnings.append(f"rule params changed without a version bump: {key}")
                plan.upserts.append(edge)
        for key, row in rows.items():
            if key in desired:
                continue
            manual = str(row.get("confidence") or "") == CONF_MANUAL
            rs = self.rulesets.get(key[3])
            if rs is None or not rs.enabled:
                plan.orphaned.append(dict(_key_dict(row), reason="unknown-ruleset" if rs is None
                                          else "ruleset-disabled"))
                if purge_orphaned:
                    (plan.protected if manual else plan.deletes).append(_key_dict(row))
                continue
            if key[3] not in active:
                continue
            if manual:
                plan.protected.append(_key_dict(row))
                continue
            if key in evaluated or (scope == "full" and result.covers(key[0], key[1])):
                plan.deletes.append(_key_dict(row))
            elif self.is_stale(row):
                plan.stale.append(_key_dict(row))
        return plan


# ---------------------------------------------------------------------------
# Small pure helpers used by the graph
# ---------------------------------------------------------------------------

def _min_opt(*vals: Optional[float]) -> Optional[float]:
    xs = [v for v in vals if v is not None]
    return min(xs) if xs else None


def _host(url: str) -> str:
    m = re.match(r"^[a-z]+://([^/]+)", url or "", re.I)
    return m.group(1).lower() if m else ""


def _evidence(role: str, m: ModelSpec, field_name: str, fv: Optional[FieldValue]) -> Evidence:
    fv = fv or FieldValue(value=None)
    return Evidence(role=role, model_id=m.model_id, field=field_name, source_url=fv.source_url,
                    source_kind=fv.source_kind, source_id=fv.source_id,
                    content_sha256=fv.content_sha256)


def _record_evidence(r: FitRecord) -> Evidence:
    return Evidence(role="record", model_id=r.b, field=r.relation,
                    source_url=r.source_url, source_kind=r.source_kind or r.origin,
                    source_id=r.source_id, content_sha256=r.content_sha256)


def _dedupe_evidence(items: Iterable[Evidence]) -> list[Evidence]:
    seen: dict[tuple, Evidence] = {}
    for e in items:
        seen.setdefault((e.role, _k(e.model_id), e.field, e.source_url, _k(e.source_id)), e)
    return list(seen.values())


def _dedupe_assumptions(items: Iterable[Assumption]) -> list[Assumption]:
    seen: dict[tuple[str, str], Assumption] = {}
    for a in items:
        seen.setdefault((a.code, a.text), a)
    return list(seen.values())


def _describe(c: Check) -> str:
    """The arithmetic behind a numeric check, from its values."""
    v = c.values
    if c.code.startswith("cold-voc-"):
        expr = (f"{v['series']} x {_fmt(v['voc_v'])} V x {_fmt(v['factor'], 4)} = "
                f"{_fmt(v['cold_voc_v'])} V at {_fmt(v['design_temp_c'])} °C")
        if c.code == "cold-voc-ok":
            return f"{expr}, within {_fmt(v['allowed_v'])} V"
        if c.code == "cold-voc-insufficient-headroom":
            return (f"{expr}: under the {_fmt(v['max_v'])} V maximum but above the "
                    f"{_fmt(v['allowed_v'])} V fit limit ({_fmt(v['headroom'] * 100)}% headroom)")
        return f"{expr}: exceeds the {_fmt(v['max_v'])} V maximum"
    if c.code == "vmp-ok":
        return (f"{v['series']} x {_fmt(v['vmp_v'])} V = {_fmt(v['string_vmp_v'])} V >= "
                f"{_fmt(v['min_v'])} V")
    if c.code == "vmp-below-min":
        return (f"{v['series']} x {_fmt(v['vmp_v'])} V = {_fmt(v['string_vmp_v'])} V is below the "
                f"{_fmt(v['min_v'])} V minimum input voltage")
    if c.code in ("current-ok", "current-exceeds-max"):
        expr = (f"{v['parallel']} x {_fmt(v['isc_a'])} A x {_fmt(v['isc_factor'])} = "
                f"{_fmt(v['design_a'])} A")
        if c.code == "current-ok":
            return f"{expr} <= {_fmt(v['max_a'])} A"
        return f"{expr} exceeds the {_fmt(v['max_a'])} A maximum input current"
    if c.code == "watts-clipped":
        return f"{_fmt(v['array_w'])} W rated, the input uses at most {_fmt(v['max_w'])} W"
    if c.code == "watts-ok":
        return f"{_fmt(v['array_w'])} W rated"
    if c.code == "rating-voltage-exceeded":
        return f"{_fmt(v['cold_voc_v'])} V exceeds the {_fmt(v['rating_v'])} V connector rating"
    if c.code == "rating-current-exceeded":
        return f"{_fmt(v['design_a'])} A exceeds the {_fmt(v['rating_a'])} A connector rating"
    if c.code == "rating-voltage-unknown":
        return (f"the {_fmt(v.get('rating_v'))} V connector rating needs the configuration's "
                "cold open-circuit voltage")
    if c.code == "rating-current-unknown":
        return (f"the {_fmt(v.get('rating_a'))} A connector rating needs the short-circuit "
                "current")
    return c.code


def _combine(checks: Sequence[Check]) -> str:
    fails = [c for c in checks if c.outcome == FAIL]
    if fails:
        if any(c.kind in _MAX_KINDS or c.code.startswith("rating-") for c in fails):
            return OVER_LIMIT
        return NO_FIT
    if any(c.outcome == UNKNOWN_OUTCOME for c in checks):
        return UNKNOWN
    return FITS


def _option_rank(o: ArrayOption) -> tuple:
    """Most usable watts, then fewest units, then fewer parallel strings,
    then the lowest cold voltage."""
    return (-(o.usable_w or 0.0), o.units, o.parallel,
            o.cold_voc_v if o.cold_voc_v is not None else 0.0, o.series)


def _record_ints(rec: FitRecord) -> dict[str, int]:
    """The record's configuration keys as positive ints (others ignored)."""
    out: dict[str, int] = {}
    for k in _RECORD_CONFIG_KEYS:
        if k in rec.config and not isinstance(rec.config[k], bool):
            try:
                n = int(rec.config[k])
            except (TypeError, ValueError):
                continue
            if n >= 1:
                out[k] = n
    return out


def _config_phrase(o: Optional[ArrayOption]) -> str:
    if o is None:
        return "?"
    s, p = o.series, o.parallel
    if s == 1 and p == 1:
        return "1 unit"
    if p == 1:
        return f"{s} in series"
    if s == 1:
        return f"{p} in parallel"
    return f"{p} parallel strings of {s} in series"


def _limit_phrase(key: str, n: int) -> str:
    if key == "max_series":
        return f"{n} in series"
    if key == "max_parallel":
        return f"{n} in parallel"
    return "1 unit" if n == 1 else f"{n} units"


def _record_matches(r: FitRecord, rs: RuleSet, a: ModelSpec, b: ModelSpec) -> bool:
    if r.relation != rs.relation:
        return False
    ka, kb = _k(a.model_id), _k(b.model_id)
    return {_k(r.a), _k(r.b)} == {ka, kb} and ka != kb


def _endorsements(rs: RuleSet, records: Iterable[FitRecord]) -> set[tuple[str, str]]:
    """(adapter id, product id) pairs that a fit record endorses."""
    conn = rs.rule_of(KIND_CONNECTOR_PATH)
    rel = str((conn.params if conn else {}).get("endorse_relation") or "")
    if not rel:
        return set()
    out: set[tuple[str, str]] = set()
    for r in records:
        if r.relation == rel and r.verdict in FIT_VERDICTS:
            out.add((_k(r.b), _k(r.a)))
            out.add((_k(r.a), _k(r.b)))
    return out


def _node_signature(m: Optional[ModelSpec], fields: set[str]) -> dict:
    if m is None:
        return {}
    out = {}
    for f in sorted(fields):
        fv = m.get(f)
        if fv is not None:
            out[f] = [fv.value, fv.unit, fv.source_url, _k(fv.source_id), fv.content_sha256,
                      fv.source_kind]
    return {"id": _k(m.model_id), "kind": m.kind, "brand": m.brand, "name": m.name, "v": out}


_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


def _numbers_in(value: Any) -> list[float]:
    """The numbers a spec value carries: itself when numeric, else every
    number written in its text ("120/240V" -> [120.0, 240.0])."""
    out: list[float] = []
    for item in _as_list(value):
        if item is None or isinstance(item, bool):
            continue
        f = _as_float(item)
        if f is not None:
            out.append(f)
            continue
        out.extend(float(x) for x in _NUMBER_RE.findall(ss.normalize_text(str(item))))
    return out


def _policy_match(rule: Mapping[str, Any], value: Any) -> bool:
    """Exclusions fail closed: a numeric test on text matches when ANY number
    in the text satisfies it ("120/240V" is >= 200)."""
    op = rule["op"]
    target = rule.get("value")
    if op == "truthy":
        return _truthy(value) is True
    if op == "matches":
        return bool(rule["_rx"].search(_fold_dashes(ss.normalize_text(str(value)))))
    if op == "in":
        vals = {str(x).casefold() for x in _as_list(target)}
        return any(str(v).casefold() in vals for v in _as_list(value))
    tnum = _as_float(target)
    nums = _numbers_in(value) if tnum is not None else []
    if nums:
        test = {"gte": lambda n: n >= tnum - _EPS, "gt": lambda n: n > tnum + _EPS,
                "lte": lambda n: n <= tnum + _EPS, "lt": lambda n: n < tnum - _EPS,
                "eq": lambda n: abs(n - tnum) <= _EPS, "ne": lambda n: abs(n - tnum) > _EPS}[op]
        return any(test(n) for n in nums)
    if op in ("eq", "ne"):
        same = str(value).casefold() == str(target).casefold()
        return same if op == "eq" else not same
    return False


def _row_config(row: Mapping[str, Any]) -> dict:
    cfg = row.get("config")
    if isinstance(cfg, str):
        try:
            cfg = json.loads(cfg)
        except ValueError:
            cfg = {}
    return cfg if isinstance(cfg, dict) else {}


def _rules_sig(rs: RuleSet) -> str:
    """One hash of every member rule's effective parameters."""
    return stable_hash({r.id: r.signature for r in rs.rules}, digest_chars=12)


def _key_dict(row: Mapping[str, Any]) -> dict:
    return {k: row.get(k) for k in _KEY_FIELDS}


# ---------------------------------------------------------------------------
# Manufacturer lists: model-name resolution (consumable fitment and friends)
# ---------------------------------------------------------------------------

def name_key(text: str, *, brand: str = "") -> str:
    """Normalised model-name key: case and punctuation folded, '+' kept as
    'plus' (an i7 and an i7+ are different models), a leading brand removed."""
    s = ss.normalize_text(str(text or "")).casefold()
    s = s.replace("+", " plus ").replace("&", " and ")
    toks = re.sub(r"[^0-9a-z]+", " ", s).split()
    if brand:
        btoks = name_key_tokens(brand)
        if btoks and toks[:len(btoks)] == btoks and len(toks) > len(btoks):
            toks = toks[len(btoks):]
    return "".join(toks)


def name_key_tokens(text: str) -> list[str]:
    s = ss.normalize_text(str(text or "")).casefold().replace("+", " plus ").replace("&", " and ")
    return re.sub(r"[^0-9a-z]+", " ", s).split()


def split_model_list(text: str) -> list[str]:
    """Split a published compatibility list into model names. A series
    prefix carries forward to bare model codes: "Series i3, i3+, j7" ->
    ["Series i3", "Series i3+", "Series j7"]. Exact resolution happens in
    :class:`ModelIndex`, so a wrong carry can only lose a match."""
    t = ss.normalize_text(str(text or ""))
    t = re.sub(r"(?i)^\s*(?:compatible\s+with|fits|works\s+with|for)\s*:?\s*", "", t)
    parts = [p.strip(" .:()[]") for p in re.split(r"\s*(?:[,;/|\n]|\band\b|&)\s*", t)]
    out: list[str] = []
    prefix = ""
    for p in parts:
        if not p:
            continue
        toks = p.split()
        if prefix and toks and re.search(r"\d", toks[0]):
            out.append(f"{prefix} {p}")
            continue
        out.append(p)
        lead = []
        for tok in toks:
            if re.search(r"\d", tok):
                break
            lead.append(tok)
        if lead and len(lead) < len(toks):
            prefix = " ".join(lead)
    return out


@dataclass(frozen=True)
class Resolution:
    name: str
    status: str                  # exact | ambiguous | none
    model_id: Any = None
    candidates: tuple = ()


class ModelIndex:
    """Exact (normalised) lookup of model names, model numbers and aliases.
    Ambiguous names resolve to nothing: a fit list must never attach to the
    wrong model."""

    def __init__(self, models: Iterable[ModelSpec], *, number_fields: Sequence[str] = ("model_number",)):
        self._keys: dict[str, set[str]] = {}
        self._ids: dict[str, Any] = {}
        self._kind: dict[str, str] = {}
        for m in models:
            mid = _k(m.model_id)
            self._ids[mid] = m.model_id
            self._kind[mid] = m.kind
            names = [m.name, *m.aliases]
            if m.brand and m.name:
                names.append(f"{m.brand} {m.name}")
            for f in number_fields:
                fv = m.get(f)
                if fv is not None:
                    names.append(str(fv.value))
            for n in names:
                if not n:
                    continue
                for key in {name_key(n), name_key(n, brand=m.brand)}:
                    if key:
                        self._keys.setdefault(key, set()).add(mid)

    def resolve(self, name: str, *, brand: str = "", kinds: Optional[Iterable[str]] = None) -> Resolution:
        keys = {name_key(name), name_key(name, brand=brand)}
        if brand:
            keys.add(name_key(f"{brand} {name}"))
        hits: set[str] = set()
        for key in keys:
            if key:
                hits |= self._keys.get(key, set())
        if kinds is not None:
            ks = set(kinds)
            hits = {h for h in hits if self._kind.get(h) in ks}
        if len(hits) == 1:
            mid = next(iter(hits))
            return Resolution(name, "exact", self._ids[mid], (self._ids[mid],))
        if hits:
            return Resolution(name, "ambiguous", None, tuple(self._ids[h] for h in sorted(hits)))
        return Resolution(name, "none")


def fit_records_from_list(subject: Any, listed: Union[str, Sequence[str]], *, index: ModelIndex,
                          relation: str, subject_side: str = "b", verdict: str = FITS,
                          brand: str = "", kinds: Optional[Iterable[str]] = None,
                          source_url: str = "", source_kind: str = "", publisher: str = "",
                          fetched_at: str = "", content_sha256: str = "", source_id: Any = None,
                          origin: str = ORIGIN_MANUFACTURER) -> tuple[list[FitRecord], list[Resolution]]:
    """FitRecords from one published list ("Compatible with: A, B and C").
    ``subject`` is the product whose page published the list (a consumable,
    a battery, an adapter); ``subject_side`` says which side of the relation
    it is on. Returns (records, unresolved names)."""
    if subject_side not in ("a", "b"):
        raise ValueError("subject_side must be 'a' or 'b'")
    names = split_model_list(listed) if isinstance(listed, str) else [str(x) for x in listed]
    recs: list[FitRecord] = []
    bad: list[Resolution] = []
    seen: set[str] = set()
    for n in names:
        res = index.resolve(n, brand=brand, kinds=kinds)
        if res.status != "exact":
            bad.append(res)
            continue
        if _k(res.model_id) in seen or _k(res.model_id) == _k(subject):
            continue
        seen.add(_k(res.model_id))
        a, b = (res.model_id, subject) if subject_side == "b" else (subject, res.model_id)
        recs.append(FitRecord(a=a, b=b, relation=relation, verdict=verdict, origin=origin,
                              source_url=source_url, source_kind=source_kind, publisher=publisher,
                              fetched_at=fetched_at, content_sha256=content_sha256,
                              source_id=source_id, statement=n))
    return recs, bad


# ---------------------------------------------------------------------------
# Write plan and the edge writer
# ---------------------------------------------------------------------------

@dataclass
class EdgePlan:
    upserts: list[Edge] = dc_field(default_factory=list)
    deletes: list[dict] = dc_field(default_factory=list)
    unchanged: list[dict] = dc_field(default_factory=list)
    recomputed: list[dict] = dc_field(default_factory=list)
    stale: list[dict] = dc_field(default_factory=list)
    protected: list[dict] = dc_field(default_factory=list)
    disabled: list[str] = dc_field(default_factory=list)
    warnings: list[str] = dc_field(default_factory=list)
    # Rows of rulesets the graph has disabled or does not know (key fields +
    # reason); deleted only with plan(..., purge_orphaned=True).
    orphaned: list[dict] = dc_field(default_factory=list)

    def summary(self) -> dict:
        return {"upserts": len(self.upserts), "deletes": len(self.deletes),
                "unchanged": len(self.unchanged), "recomputed": len(self.recomputed),
                "stale": len(self.stale), "protected": len(self.protected),
                "orphaned": len(self.orphaned),
                "disabled": list(self.disabled), "warnings": list(self.warnings)}


_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")


def _ident(name: str) -> str:
    if not _IDENT_RE.match(name or ""):
        raise CompatGraphError(f"invalid SQL identifier {name!r}")
    return name


def reference_ddl(table: str = "compat_edges", *, model_id_type: str = "bigint",
                  source_id_type: str = "bigint") -> str:
    """The compat_edges shape this module writes (a site migration may add
    foreign keys to its models / spec_sources tables)."""
    t = _ident(table)
    for typ in (model_id_type, source_id_type):
        if not re.fullmatch(r"[a-z ]+", typ):
            raise CompatGraphError(f"invalid SQL type {typ!r}")
    verdicts = ", ".join(f"'{v}'" for v in VERDICTS)
    confs = ", ".join(f"'{c}'" for c in CONFIDENCES)
    return (f"CREATE TABLE IF NOT EXISTS {t} (\n"
            f"  id bigserial PRIMARY KEY,\n"
            f"  a_model_id {model_id_type} NOT NULL,\n"
            f"  b_model_id {model_id_type} NOT NULL,\n"
            f"  relation text NOT NULL,\n"
            f"  verdict text NOT NULL CHECK (verdict IN ({verdicts})),\n"
            f"  config jsonb NOT NULL DEFAULT '{{}}'::jsonb,\n"
            f"  via_model_id {model_id_type},\n"
            f"  rule_id text NOT NULL,\n"
            f"  rule_version text NOT NULL,\n"
            f"  evidence_source_ids {source_id_type}[] NOT NULL DEFAULT '{{}}',\n"
            f"  confidence text NOT NULL CHECK (confidence IN ({confs})),\n"
            f"  computed_at timestamptz NOT NULL DEFAULT now(),\n"
            f"  UNIQUE (a_model_id, b_model_id, relation, rule_id)\n"
            f");\n"
            f"CREATE INDEX IF NOT EXISTS {t.replace('.', '_')}_b_idx ON {t} (b_model_id, relation);\n")


class EdgeWriter:
    """Applies an :class:`EdgePlan` to a site's compat_edges table through a
    DB-API connection the caller owns (psycopg2). Manual rows are protected
    in SQL as well as in the plan."""

    def __init__(self, conn: Any, *, cfg: Optional[Mapping[str, Any]] = None,
                 table: str = "", columns: Optional[Mapping[str, str]] = None,
                 page_size: Optional[int] = None):
        wcfg = dict((cfg or DEFAULTS).get("writer") or {})
        self.conn = conn
        self.table = _ident(table or str(wcfg.get("table") or "compat_edges"))
        cols = dict(wcfg.get("columns") or {})
        cols.update(columns or {})
        self.cols = {f: _ident(str(cols.get(f) or f)) for f in _ROW_FIELDS}
        self.page_size = int(page_size or wcfg.get("page_size") or 500)

    def select_sql(self) -> str:
        c = self.cols
        names = ", ".join(f"{c[f]} AS {f}" for f in ("a_model_id", "b_model_id", "relation",
                                                      "rule_id", "rule_version", "verdict",
                                                      "via_model_id", "confidence", "config"))
        return f"SELECT {names} FROM {self.table} WHERE (%(all)s OR {c['rule_id']} = ANY(%(rule_ids)s))"

    def upsert_sql(self) -> str:
        c = self.cols
        cols = ", ".join(c[f] for f in _ROW_FIELDS)
        vals = []
        for f in _ROW_FIELDS:
            cast = "::jsonb" if f == "config" else "::timestamptz" if f == "computed_at" else ""
            vals.append(f"%({f})s{cast}")
        keys = ", ".join(c[f] for f in _KEY_FIELDS)
        sets = ", ".join(f"{c[f]} = EXCLUDED.{c[f]}" for f in _ROW_FIELDS if f not in _KEY_FIELDS)
        return (f"INSERT INTO {self.table} ({cols}) VALUES ({', '.join(vals)}) "
                f"ON CONFLICT ({keys}) DO UPDATE SET {sets} "
                f"WHERE {self.table}.{c['confidence']} IS DISTINCT FROM '{CONF_MANUAL}' "
                f"OR EXCLUDED.{c['confidence']} = '{CONF_MANUAL}'")

    def delete_sql(self) -> str:
        c = self.cols
        where = " AND ".join(f"{c[f]} = %({f})s" for f in _KEY_FIELDS)
        return (f"DELETE FROM {self.table} WHERE {where} "
                f"AND {c['confidence']} IS DISTINCT FROM '{CONF_MANUAL}'")

    @staticmethod
    def params(edge: Edge) -> dict:
        row = edge.to_row()
        row["config"] = json.dumps(row["config"], sort_keys=True, default=str)
        row["computed_at"] = row["computed_at"] or _now_iso()
        return row

    def load_existing(self, *, rule_ids: Optional[Iterable[str]] = None) -> list[dict]:
        ids = sorted({str(r) for r in rule_ids}) if rule_ids is not None else []
        with self.conn.cursor() as cur:
            cur.execute(self.select_sql(), {"all": rule_ids is None, "rule_ids": ids})
            names = [d[0] for d in cur.description]
            return [dict(zip(names, row)) for row in cur.fetchall()]

    def apply(self, plan: EdgePlan, *, commit: bool = True) -> dict:
        ups = [self.params(e) for e in plan.upserts]
        dels = [{k: d.get(k) for k in _KEY_FIELDS} for d in plan.deletes]
        try:
            with self.conn.cursor() as cur:
                _run_batch(cur, self.upsert_sql(), ups, self.page_size)
                _run_batch(cur, self.delete_sql(), dels, self.page_size)
            if commit:
                self.conn.commit()
        except Exception:
            if commit:
                self.conn.rollback()
            raise
        return {"upserted": len(ups), "deleted": len(dels)}


def _run_batch(cur: Any, sql: str, rows: Sequence[Mapping[str, Any]], page_size: int) -> None:
    if not rows:
        return
    try:
        from psycopg2.extras import execute_batch  # type: ignore[import-untyped]
    except ImportError:  # pragma: no cover - any DB-API driver works, just slower
        cur.executemany(sql, rows)
        return
    execute_batch(cur, sql, rows, page_size=page_size)


# ---------------------------------------------------------------------------
# CLI (offline, read-only)
# ---------------------------------------------------------------------------

def _cli_cfg(args: argparse.Namespace) -> dict:
    file_cfg = json.loads(Path(args.config_file).read_text()) if args.config_file else None
    site_cfg = {"profile": args.profile} if args.profile else None
    return resolve_config(site=args.site or "", agent_id=args.agent_id or "", site_cfg=site_cfg,
                          file_cfg=file_cfg)


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="python3 -m framework.core.compat_graph",
                                description="Compatibility graph (offline, read-only tools).")
    p.add_argument("--config-file", help="config JSON to use instead of storage/repo config")
    p.add_argument("--site", default="")
    p.add_argument("--agent-id", default="")
    p.add_argument("--profile", default="")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("config", help="print the resolved config and its problems")
    sub.add_parser("rules", help="list rulesets with their composite rule versions")
    pf = sub.add_parser("factor", help="cold open-circuit voltage factor for one panel")
    pf.add_argument("--temp", type=float, default=DEFAULT_DESIGN_TEMP_C, help="design temperature °C")
    pf.add_argument("--tc", type=float, default=None, help="Voc temperature coefficient, %%/°C")
    pe = sub.add_parser("eval", help="evaluate models from a JSON file {models, fit_records}")
    pe.add_argument("file")
    pe.add_argument("--zip", default="", help="evaluate at this ZIP's design temperature")
    pe.add_argument("--ruleset", action="append", default=None)
    pe.add_argument("--all", action="store_true", help="also print edges that would not be stored")
    pd = sub.add_parser("ddl", help="print the reference compat_edges DDL")
    pd.add_argument("--table", default="compat_edges")
    args = p.parse_args(argv)

    if args.cmd == "ddl":
        print(reference_ddl(args.table))
        return 0
    if args.cmd == "factor":
        nec = nec_factor(args.temp)
        out: dict[str, Any] = {"design_temp_c": args.temp, "nec_690_7_a": nec}
        if args.tc is not None:
            out["temperature_coefficient"] = coefficient_factor(args.tc, args.temp)
        print(json.dumps(out, indent=2))
        return 0
    cfg = _cli_cfg(args)
    graph = CompatGraph(cfg)
    if args.cmd == "config":
        shown = {k: v for k, v in cfg.items() if not k.startswith("_")}
        print(json.dumps({"config": shown, "problems": graph.problems}, indent=2, sort_keys=True,
                         default=str))
        return 0
    if args.cmd == "rules":
        print(json.dumps({rs.id: {"relation": rs.relation, "a_kinds": list(rs.a_kinds),
                                  "b_kinds": list(rs.b_kinds), "rule_version": rs.composite_version,
                                  "enabled": rs.enabled, "problems": list(rs.problems)}
                          for rs in graph.rulesets.values()}, indent=2))
        return 0
    doc = json.loads(Path(args.file).read_text())
    models = [ModelSpec.from_dict(m) for m in doc.get("models") or []]
    records = [FitRecord.from_dict(r) for r in doc.get("fit_records") or []]
    climate = graph.climate(zip_code=args.zip) if args.zip else None
    res = graph.build(models, records, rulesets=args.ruleset, climate=climate)
    edges = res.edges if args.all else res.stored()
    print(json.dumps({"summary": res.summary(), "edges": [e.to_dict() for e in edges],
                      "skipped": [s.to_dict() for s in res.skipped], "unresolved": res.unresolved},
                     indent=2, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
