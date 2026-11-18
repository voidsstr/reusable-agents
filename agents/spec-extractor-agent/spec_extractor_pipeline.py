"""One spec-extractor run: sources -> extract -> validate -> compat -> runtime.

Built on two framework primitives, configured per site and per product kind:

  framework.core.spec_sources   polite fetching (robots.txt, per-host rate
                                limit, conditional GETs, content hashes),
                                documents, deterministic extractors, the
                                batched cheap-model fallback, consolidation
                                with provenance, the adjudication queue;
  framework.core.compat_graph   versioned compatibility rules -> compat_edges.

PHASES
------
1. sources   Fetch the sources that are due: never fetched, older than
             ``sources.refetch_hours``, extracted with an older config
             fingerprint, or never extracted. Failed sources back off
             exponentially; a refused (Amazon) URL is never fetched again;
             a deferred host is simply tried next run. Order: never-fetched
             and config-changed first, then the site's priority models, then
             oldest check; hosts interleaved so the per-host interval
             overlaps. Bounded by ``max_fetches_per_run`` and
             ``max_run_minutes``.
2. extract   For each fetched source whose content hash or extraction
             fingerprint changed: build the document (Amazon pages refused)
             and run the brand's deterministic extractors. The candidate
             values are stored on the source row, so an unchanged source is
             never parsed again (needs_extraction short-circuit). Then, when
             the kind's ``llm.enabled``, one batched cheap-model pass (10 per
             call, pool reserve) for fields the deterministic pass missed;
             its values are kept with the source too. LLM answers survive a
             re-extraction of UNCHANGED content (they still quote it) and are
             replaced field by field only when a new pass on that source
             completes; when the LLM config changes, every field the source's
             earlier answers covered is asked again. A deferred pass (pool
             reserve, provider) keeps the earlier answers: nothing blanks.
3. validate  Settle queued conflicts within the daily adjudication budget
             (disabled unless configured), then consolidate every model whose
             inputs changed (short_circuit.partition_by_hash over a hash of
             its sources' candidates, operator values, adjudication decisions
             and config; unchanged models are revisited every
             ``revisit_after_runs`` runs). A model's spec_values, typed spec
             row and status are written in one transaction, and only when
             they differ. New conflicts go to the adjudication queue; settled
             or vanished ones are retired.
4. compat    (``compat.enabled``) Rebuild compat_edges from accepted values
             with framework.core.compat_graph: incremental for changed
             models, a full rebuild on a config / rule-version change and
             every ``full_rebuild_every_runs`` runs, skipped when nothing
             changed. Batched by A-side models so memory stays bounded. A
             withdrawn fit record or list entry loses its edge on the next
             run; the manual rows the engine writes from operator fit
             records are its own and are released once no longer derived
             (CompatPhase).
5. runtime   (``runtime.enabled``) Per spec-complete model of the runtime
             kinds and per load preset: usable energy (measured, else rated
             x factor with an interval) over the preset's average draw, only
             for loads the model's continuous output covers; peak check
             against the preset's surge. Rows of models that left the scope
             (retired, another kind) are deleted.

AGENT_FORCE_RUN=1 (framework.core.short_circuit.force_run_requested)
bypasses the in-run short-circuits: every fetched source is re-extracted,
every model re-consolidated and compat fully rebuilt (sources still fall due
by age: politeness is not a short-circuit). ENGINE_VERSION is part of every
model's input hash, so a change to how this engine consolidates reaches
unchanged models on the next run.

Nothing parses Amazon text: documents and candidates from Amazon are
refused by the primitive, and the writer drops any row whose source is an
Amazon URL as a second line of defence.
"""
from __future__ import annotations

import collections
import json
import time
import urllib.parse
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Optional, Sequence

from framework.core import compat_graph as cg
from framework.core import spec_sources as ss
from framework.core.short_circuit import force_run_requested, partition_by_hash, stable_hash

from spec_extractor_settings import KindSettings, Settings, merge
from spec_extractor_store import OPERATOR_PREFIX, ModelRow, SourceRow, SpecStore, num

Say = Callable[..., None]
Decide = Callable[..., None]

STATE_VERSION = 1
#: Bump when a change here alters a model's outputs for the same inputs: it
#: is part of every model's input hash, so every model is consolidated again.
ENGINE_VERSION = "2"
BODY_STATUSES = frozenset(ss.FetchResult.BODY_STATUSES)
UNCHANGED_STATUSES = frozenset({"unchanged", "not-modified", "cached"})
GONE_HTTP = frozenset({404, 410})
UNHEALTHY = frozenset({"failed", "gone", "blocked", "refused"})
LLM_SIG_KEYS = ("model", "model_tier", "provider", "confidence", "min_quote_chars",
                "max_chars_per_item", "context_lines", "extractor_label")
LLM_MARKER = "#spec-source-"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(s: Any) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def host_of(url: str) -> str:
    return (urllib.parse.urlsplit(url or "").hostname or "").lower()


# ---------------------------------------------------------------------------
# Candidate (de)serialisation: the source row keeps them, rank included
# ---------------------------------------------------------------------------

def cand_to_dict(c: ss.Candidate) -> dict:
    d = c.to_dict()
    d["rank"] = [int(c.rank[0]), int(c.rank[1])]
    return d


def cand_from_dict(d: Mapping[str, Any]) -> ss.Candidate:
    c = ss.Candidate.from_dict(d)
    r = d.get("rank")
    if isinstance(r, (list, tuple)) and len(r) == 2:
        c = replace(c, rank=(int(r[0]), int(r[1])))
    return c


def is_llm_candidate(d: Mapping[str, Any]) -> bool:
    """A stored candidate the cheap-model pass produced (its extractor id is
    always ``llm:<model>``; the extractor label is configurable)."""
    return d.get("extractor") == ss.EXTRACTOR_LLM or str(d.get("extractor_id") or "").startswith("llm:")


def candidates_sha(cands: Sequence[Mapping[str, Any]]) -> str:
    return stable_hash(sorted((dict(c) for c in cands), key=lambda d: repr(sorted(d.items()))),
                       float_precision=6)


# ---------------------------------------------------------------------------
# Collaborators (tests inject fakes; nothing here runs on import)
# ---------------------------------------------------------------------------

@dataclass
class Collaborators:
    transport: Optional[Callable] = None          # spec_sources Transport
    cache_dir: Optional[str] = None
    clock: Callable[[], datetime] = utcnow
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    pdf_converter: Optional[Callable[[bytes], str]] = None
    chat: Optional[Callable[..., str]] = None     # LLM + adjudication chat
    admit: Optional[Callable[[str], bool]] = None
    spec_file_cfg: Optional[Mapping[str, Any]] = None    # skip the storage read
    compat_file_cfg: Optional[Mapping[str, Any]] = None


@dataclass
class RunReport:
    status: str = "success"
    summary: str = ""
    summary_md: str = ""
    metrics: dict = field(default_factory=dict)
    state: dict = field(default_factory=dict)
    error_text: str = ""
    short_circuited: bool = False


# ---------------------------------------------------------------------------
# Per-kind engines built from the layered spec_sources config
# ---------------------------------------------------------------------------

@dataclass
class KindEngine:
    kind: str
    settings: KindSettings
    cfg: dict
    ss: ss.SpecSources

    @property
    def required(self) -> tuple[str, ...]:
        return self.ss.fields.required()

    @property
    def llm_enabled(self) -> bool:
        return bool((self.cfg.get("llm") or {}).get("enabled"))

    def consolidation_fp(self, settings: Settings) -> str:
        """Everything (besides the inputs) that shapes a model's outputs."""
        t = self.settings.typed
        return stable_hash({
            "fields": self.cfg.get("fields") or {}, "rules": self.cfg.get("rules") or [],
            "consolidate": self.cfg.get("consolidate") or {},
            "priority": self.cfg.get("source_kind_priority") or {},
            "required": list(self.required),
            "typed": {"table": t.name, "columns": dict(t.columns), "key": self.settings.typed_key,
                      "completeness": self.settings.typed_completeness} if t else None,
            "values": dict(settings.values.columns),
            "status": dict(settings.model_status_columns),
        }, float_precision=9)

    def llm_signature(self, state: Mapping[str, Any]) -> str:
        llm = self.cfg.get("llm") or {}
        return stable_hash({"llm": {k: llm.get(k) for k in LLM_SIG_KEYS},
                            "content": state.get("extracted_sha") or "",
                            "fp": state.get("fingerprint") or ""})


def build_engines(settings: Settings, *, storage, collab: Collaborators
                  ) -> tuple[dict[str, KindEngine], dict, ss.PoliteFetcher, list[str]]:
    """Resolve the common layer and one config per kind. Returns (engines,
    common cfg, the shared fetcher, problems). One fetcher serves every kind
    so the per-host rate limit holds across kinds."""
    problems: list[str] = []
    if collab.spec_file_cfg is not None:
        file_cfg = dict(collab.spec_file_cfg)
    else:
        file_cfg = ss.load_file_config(storage, problems=problems)
    common_layer = dict(settings.block.get("spec_sources") or {})
    common = ss.resolve_config(site=settings.site, agent_id=settings.agent_id,
                               site_cfg=common_layer, file_cfg=file_cfg)
    problems += [f"spec_sources: {p}" for p in common.get("config_problems") or []]
    fetcher = ss.PoliteFetcher(common.get("fetch"), refusal=ss.AmazonRefusal(common.get("refuse")),
                               transport=collab.transport, cache_dir=collab.cache_dir,
                               clock=collab.clock, monotonic=collab.monotonic, sleep=collab.sleep,
                               doc_cfg=common.get("documents"))
    problems += [f"fetch: {p}" for p in fetcher.problems]
    engines: dict[str, KindEngine] = {}
    primary = set((settings.block.get("metrics") or {}).get("primary_kinds") or []) or set(settings.kinds)
    for kind, ks in settings.kinds.items():
        layer = merge(common_layer, ks.spec_sources)
        cfg = ss.resolve_config(site=settings.site, agent_id=settings.agent_id,
                                site_cfg=layer, file_cfg=file_cfg)
        kp = [f"kind {kind}: {p}" for p in cfg.get("config_problems") or []]
        fields = dict(cfg.get("fields") or {})
        if ks.fields:
            unknown = [f for f in ks.fields if f not in fields]
            kp += [f"kind {kind}: field {f!r} is not in the spec_sources profile" for f in unknown]
            fields = {f: fields[f] for f in ks.fields if f in fields}
        if ks.required is not None:
            unknown = [f for f in ks.required if f not in fields]
            kp += [f"kind {kind}: required field {f!r} is not one of its fields" for f in unknown]
            fields = {f: dict(spec or {}, required=f in ks.required) for f, spec in fields.items()}
        if not fields:
            kp.append(f"kind {kind}: no spec fields (select a profile with fields)")
        elif kind in primary and not any((spec or {}).get("required") for spec in fields.values()):
            # completeness would be 1.0 for every model: the goal would count nothing real
            kp.append(f"kind {kind}: no required fields, so spec-completeness is undefined; a primary "
                      f"kind needs kinds.{kind}.required (the fields that make it spec-complete)")
        cfg["fields"] = fields
        cfg["rules"] = [r for r in cfg.get("rules") or []
                        if r.get("a") in fields and r.get("b") in fields]
        # fetching, refusal and adjudication are site-wide: the common layer's
        for key in ("fetch", "refuse", "adjudication"):
            cfg[key] = common.get(key)
        cfg["config_problems"] = []
        eng = ss.SpecSources(cfg, fetcher=fetcher, pdf_converter=collab.pdf_converter)
        kp += [f"kind {kind}: {p}" for p in eng.problems if not p.startswith("fetch")]
        if ks.typed:
            missing = sorted(set(ks.typed.columns) - set(fields))
            kp += [f"kind {kind}: typed column for unknown field {f!r}" for f in missing]
        problems += kp
        engines[kind] = KindEngine(kind=kind, settings=ks, cfg=cfg, ss=eng)
    return engines, common, fetcher, problems


# ---------------------------------------------------------------------------
# Source scheduling
# ---------------------------------------------------------------------------

def backoff_hours(failures: int, base: float, cap: float) -> float:
    return float(min(cap, base * (2 ** max(0, int(failures) - 1))))


def due_reasons(src: SourceRow, *, now: datetime, refetch_hours: float,
                fingerprint: str) -> list[str]:
    """Why ``src`` should be fetched this run ([] = not due)."""
    st = src.state
    if st.get("status") == "refused" and st.get("refused_url") == src.url:
        return []
    nxt = parse_iso(st.get("next_attempt_at"))
    if nxt is not None and now < nxt:
        return []
    fetch = st.get("fetch") or {}
    reasons = []
    checked = parse_iso(fetch.get("checked_at"))
    if fetch.get("url") not in (None, "", src.url) and fetch.get("url") != ss.normalize_url(src.url):
        reasons.append("url-changed")
    if checked is None:
        reasons.append("never-fetched")
    elif now - checked >= timedelta(hours=float(refetch_hours)):
        reasons.append("refetch")
    if not st.get("extracted_sha"):
        reasons.append("not-extracted")
    elif fetch.get("content_sha256") and fetch["content_sha256"] != st["extracted_sha"]:
        reasons.append("not-extracted")      # fetched content a past run could not extract
    elif st.get("fingerprint") != fingerprint:
        reasons.append("config-changed")
    return reasons


def order_due(items: list[tuple[SourceRow, list[str]]], *, priority: Mapping[str, int]
              ) -> list[tuple[SourceRow, list[str]]]:
    """Urgent first (never fetched / never extracted / config changed), then
    the site's priority models, then oldest check; then interleave hosts so
    one slow host does not serialise the run."""
    urgent = {"never-fetched", "not-extracted", "config-changed", "url-changed"}

    def key(it: tuple[SourceRow, list[str]]):
        src, reasons = it
        checked = parse_iso((src.state.get("fetch") or {}).get("checked_at"))
        return (0 if urgent & set(reasons) else 1, priority.get(src.model_key, 10 ** 9),
                checked.timestamp() if checked else 0.0, str(src.id))

    items = sorted(items, key=key)
    by_host: "collections.OrderedDict[str, list]" = collections.OrderedDict()
    for it in items:
        by_host.setdefault(host_of(it[0].url), []).append(it)
    out: list[tuple[SourceRow, list[str]]] = []
    while by_host:
        for h in list(by_host):
            out.append(by_host[h].pop(0))
            if not by_host[h]:
                del by_host[h]
    return out


# ---------------------------------------------------------------------------
# Operator values and consolidation helpers
# ---------------------------------------------------------------------------

def operator_candidate(row: Mapping[str, Any], src: Optional[SourceRow],
                       fields: ss.FieldRegistry) -> Optional[ss.Candidate]:
    """An operator-entered spec_values row as a manual candidate (it always
    wins consolidation). Values are converted to the field's canonical unit
    when the row names another unit of the same family."""
    fname = str(row.get("field") or "")
    if fname not in fields:
        return None
    spec = fields[fname]
    value_num = num(row.get("value_num"))
    value_text = row.get("value_text")
    unit = row.get("unit") or (spec.unit or None)
    if value_num is not None and spec.unit and row.get("unit"):
        try:
            if ss.canonical_unit(str(row["unit"])) != spec.unit:
                value_num = ss.convert(value_num, str(row["unit"]), spec.unit)
                unit = spec.unit
        except (ValueError, KeyError):
            pass
    if spec.kind == "bool":
        t = str(value_text or "").strip().lower()
        if t in ("true", "false"):
            value_num = 1.0 if t == "true" else 0.0
        elif value_num is not None:
            value_text = "true" if value_num else "false"
    ref = ss.SourceRef(url=src.url if src else "", kind=src.kind if src else "manual-entry",
                       publisher=(src.publisher if src else "") or "operator",
                       fetched_at=src.fetched_at if src else "",
                       content_sha256=src.content_sha256 if src else "")
    conf = num(row.get("confidence"))
    # rank (-1, 0): an operator row that cites a document we also extract
    # from must head that source's own candidates (consolidate picks one value
    # per source before "manual always wins" applies).
    return ss.Candidate(field=fname, value_num=value_num, value_text=value_text, unit=unit,
                        source=ref, extractor=ss.EXTRACTOR_MANUAL,
                        extractor_id=f"{OPERATOR_PREFIX}:{row.get('id')}",
                        confidence=1.0 if conf is None else conf,
                        snippet=str(row.get("snippet") or "")[:240],
                        qualifier=str(row.get("qualifier") or ""), rank=(-1, 0))


def model_status(spec: ss.SpecSet, has_inputs: bool) -> str:
    if not has_inputs:
        return "unsourced"
    if spec.completeness >= 1.0:
        return "complete"
    if any(spec.fields.get(f) and spec.fields[f].status == ss.STATUS_CONFLICT for f in spec.required):
        return "conflict"
    return "partial" if spec.completeness > 0 else "missing"


def engine_value_rows(spec: ss.SpecSet, model: ModelRow, source_ids: Mapping[str, Any]
                      ) -> tuple[list[dict], dict[str, str], int]:
    """(engine rows to store, {operator row id: status}, rows dropped as
    Amazon-sourced or unmapped). Mirrors spec_sources.to_db_rows, plus the
    optional provenance columns."""
    rows: list[dict] = []
    operator: dict[str, str] = {}
    dropped = 0
    for r in spec.provenance():
        if (r.extractor_id or "").startswith(OPERATOR_PREFIX + ":"):
            operator[r.extractor_id.split(":", 1)[1]] = r.status
            continue
        if ss.is_amazon_url(r.source_url) or "amazon-source" in r.reasons:
            dropped += 1
            continue
        sid = source_ids.get(r.source_url)
        if sid is None:
            dropped += 1
            continue
        rows.append({"model_id": model.id, "field": r.field, "value_num": r.value_num,
                     "value_text": r.value_text, "unit": r.unit, "source_id": sid,
                     "extractor": r.extractor, "extractor_id": r.extractor_id or "engine",
                     "confidence": round(float(r.confidence), 4), "status": r.status,
                     "snippet": (r.snippet or "")[:240] or None, "qualifier": r.qualifier or None,
                     "conflict_id": r.conflict_id or None})
    return rows, operator, dropped


# ---------------------------------------------------------------------------
# Runtime estimates (capacity over draw), pure
# ---------------------------------------------------------------------------

class RuntimeEstimator:
    """usable energy = measured, else rated x factor (interval rated x
    [low, high]); hours = usable / draw, where draw is energy_per_day / 24,
    else power x duty cycle, else power. A preset is estimated only when the
    model's continuous output covers the preset's running power; peak_ok is
    None when the preset has no surge or the model's peak is unknown."""

    def __init__(self, cfg: Mapping[str, Any]):
        en = cfg.get("energy") or {}
        self.rated_field = str(en.get("rated_field") or "")
        self.measured_field = str(en.get("measured_field") or "")
        # The usable share of a rated capacity is niche data: no default here
        # (settings require both with rated_field).
        if self.rated_field and (en.get("rated_factor") is None or not en.get("rated_interval")):
            raise ValueError("runtime.energy.rated_factor and rated_interval are required with rated_field")
        self.factor = float(en.get("rated_factor") or 0.0)
        self.rated_iv = tuple(float(x) for x in (en.get("rated_interval") or [0.0, 0.0]))
        self.measured_iv = tuple(float(x) for x in (en.get("measured_interval") or [1.0, 1.0]))
        labels = en.get("basis_labels") or {}
        self.label_measured = str(labels.get("measured") or "measured")
        self.label_rated = str(labels.get("rated") or "rated-x-{factor}").replace(
            "{factor}", _fmt_num(self.factor))
        self.cont_field = str(cfg.get("continuous_field") or "")
        self.peak_field = str(cfg.get("peak_field") or "")
        self.round = int(cfg.get("round_hours", 2))

    @property
    def fields(self) -> list[str]:
        return [f for f in (self.rated_field, self.measured_field, self.cont_field, self.peak_field) if f]

    def energy(self, values: Mapping[str, Any]) -> Optional[tuple[str, float, float, float]]:
        meas = _to_unit(values.get(self.measured_field), "Wh") if self.measured_field else None
        if meas is not None and meas > 0:
            return (self.label_measured, meas, meas * self.measured_iv[0], meas * self.measured_iv[1])
        rated = _to_unit(values.get(self.rated_field), "Wh") if self.rated_field else None
        if rated is not None and rated > 0:
            return (self.label_rated, rated * self.factor, rated * self.rated_iv[0],
                    rated * self.rated_iv[1])
        return None

    def rows(self, model_id: Any, values: Mapping[str, Any], presets: Sequence[Mapping[str, Any]]
             ) -> list[dict]:
        en = self.energy(values)
        cont = _to_unit(values.get(self.cont_field), "W")
        if en is None or cont is None:
            return []
        peak = _to_unit(values.get(self.peak_field), "W") if self.peak_field else None
        basis, usable, lo, hi = en
        out = []
        for p in presets:
            power = num(p.get("power_w"))
            if power is None or power <= 0 or cont < power:
                continue
            per_day = num(p.get("energy_per_day_wh"))
            duty = num(p.get("duty_cycle"))
            if per_day is not None and per_day > 0:
                draw = per_day / 24.0
            elif duty is not None and 0 < duty <= 1:
                draw = power * duty
            else:
                draw = power
            if draw <= 0:
                continue
            surge = num(p.get("peak_w"))
            peak_ok = None if (surge is None or surge <= 0 or peak is None) else bool(peak >= surge)
            out.append({"model_id": model_id, "preset_id": p["id"], "basis": basis,
                        "usable_energy": round(usable, 1),
                        "hours_est": round(usable / draw, self.round),
                        "hours_low": round(lo / draw, self.round),
                        "hours_high": round(hi / draw, self.round), "peak_ok": peak_ok})
        return out


def _fmt_num(v: float) -> str:
    return f"{v:.6g}"


def _to_unit(fv: Any, unit: str) -> Optional[float]:
    """A compat_graph FieldValue (or raw number) in ``unit``."""
    if fv is None:
        return None
    value = getattr(fv, "value", fv)
    if isinstance(value, bool):
        return None
    v = num(value)
    if v is None:
        return None
    u = getattr(fv, "unit", "") or ""
    if u and ss.canonical_unit(u) != ss.canonical_unit(unit):
        try:
            return float(ss.convert(v, u, unit))
        except (ValueError, KeyError):
            return None
    return v


def _row_tuple(r: Mapping[str, Any]) -> tuple:
    return (str(r.get("preset_id")), r.get("basis"), _r(r.get("usable_energy"), 1),
            _r(r.get("hours_est"), 3), _r(r.get("hours_low"), 3), _r(r.get("hours_high"), 3),
            r.get("peak_ok"))


def _r(v: Any, nd: int) -> Optional[float]:
    f = num(v)
    return None if f is None else round(f, nd)


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

class SpecExtractorRun:
    def __init__(self, settings: Settings, store: SpecStore, *, storage, collab: Collaborators,
                 prior_state: Mapping[str, Any], say: Say, decide: Decide,
                 dry_run: bool = False, only_models: Optional[Sequence[str]] = None):
        self.s = settings
        self.store = store
        self.storage = storage
        self.c = collab
        self.prior = dict(prior_state or {})
        self.say = say
        self.decide = decide
        self.dry_run = dry_run
        self.only_models = {str(m) for m in only_models} if only_models else None
        self.metrics: dict[str, float] = collections.defaultdict(float)
        self.notes: list[str] = []
        self.warnings: list[str] = []
        self.problems: list[str] = []
        self.state: dict[str, Any] = {}
        self.now = collab.clock()
        self.engines: dict[str, KindEngine] = {}
        self.models: list[ModelRow] = []
        self.by_key: dict[str, ModelRow] = {}
        self.sources: list[SourceRow] = []
        self.src_by_model: dict[str, list[SourceRow]] = collections.defaultdict(list)
        self.operator_rows: dict[str, list[dict]] = collections.defaultdict(list)
        self.fetched: dict[str, ss.FetchResult] = {}
        self.queue: Optional[ss.AdjudicationQueue] = None
        self.decisions: dict[str, list[ss.Decision]] = collections.defaultdict(list)
        self.common: dict = {}
        self.forced = force_run_requested() and not dry_run

    # -- small helpers -------------------------------------------------
    def m(self, key: str, value: float = 1.0) -> None:
        self.metrics[key] += value

    def _brand(self, model: ModelRow, eng: KindEngine, url: str = "") -> str:
        b = (self.s.block.get("brands") or {}).get("map", {}).get(model.brand, model.brand)
        return eng.ss.extractors.resolve_brand(b, url)

    def _brand_allowed(self, slug: str) -> bool:
        br = self.s.block.get("brands") or {}
        only, skip = set(br.get("only") or []), set(br.get("skip") or [])
        if only and slug not in only:
            return False
        return slug not in skip

    def _fingerprint(self, eng: KindEngine, slug: str) -> str:
        return eng.ss.extractors.fingerprint(slug)

    def _ensure_db(self, when: str) -> None:
        """Long HTTP / LLM phases can outlive a server or load-balancer idle
        timeout: probe, and reconnect (taking the run lock again) when the
        connection dropped. A lost lock raises: the run stops, its state kept."""
        if self.store.ensure_open():
            self.m("db_reconnects")
            self.notes.append(f"the site DB connection dropped {when}: reconnected and re-locked")

    # ------------------------------------------------------------------
    def run(self) -> RunReport:
        rep = RunReport()
        t0 = self.c.monotonic()
        self.say("1/5 sources: loading config", progress=0.02)
        self.engines, self.common, _fetcher, problems = build_engines(
            self.s, storage=self.storage, collab=self.c)
        if problems:
            rep.status = "failure"
            rep.summary = f"spec-extractor config problems — nothing done: {'; '.join(problems)[:400]}"
            rep.error_text = "\n".join(problems)[:4000]
            rep.metrics = {"config_problems": float(len(problems))}
            rep.state = dict(self.prior)
            return rep
        for kind, eng in self.engines.items():
            if not eng.required:
                self.warnings.append(f"kind {kind}: no required fields, so spec-completeness is undefined: "
                                     "its models count as complete once they have a source or an operator value")
        if self.forced:
            self.notes.append("AGENT_FORCE_RUN: every fetched source re-extracted, every model "
                              "re-consolidated, compat fully rebuilt")
        self._load()
        self._phase_sources(t0)
        self._ensure_db("after fetching")
        self._phase_extract()
        self._ensure_db("after extraction")
        self._phase_validate()
        self._ensure_db("before compat")
        compat_ok = self._phase_compat()
        self._ensure_db("before runtime")
        self._phase_runtime()
        self._final_metrics()
        rep.metrics = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in self.metrics.items()}
        rep.state = self.state
        did = (self.metrics.get("sources_attempted", 0) or self.metrics.get("models_written", 0)
               or self.metrics.get("compat_upserts", 0) or self.metrics.get("compat_deletes", 0)
               or self.metrics.get("runtime_rows_written", 0) or self.metrics.get("runtime_rows_deleted", 0)
               or self.metrics.get("llm_calls", 0) or self.metrics.get("adjudication_decisions", 0))
        rep.short_circuited = not did and not self.forced
        if self.problems or not compat_ok:
            rep.status = "failure"
            rep.error_text = "\n".join(self.problems)[:4000]
        rep.summary = self._summary(rep)
        rep.summary_md = self._summary_md(rep)
        return rep

    # ------------------------------------------------------------------
    def _load(self) -> None:
        models = self.store.load_models(self.engines)
        if self.only_models is not None:
            models = [m for m in models if m.key in self.only_models]
        self.models = models
        self.by_key = {m.key: m for m in models}
        self.sources = self.store.load_sources([m.id for m in models])
        for src in self.sources:
            self.src_by_model[src.model_key].append(src)
        for row in self.store.load_operator_values([m.id for m in models]):
            self.operator_rows[str(row["model_id"])].append(row)
        self.store.end_read()
        self.metrics["models_total"] = float(len(models))
        self.metrics["sources_total"] = float(len(self.sources))
        self.metrics["operator_values"] = float(sum(len(v) for v in self.operator_rows.values()))
        # one adjudication queue per agent, settings from the common layer
        adj = self.common.get("adjudication") or {}
        key = str(adj.get("queue_key") or "agents/{agent_id}/spec-adjudication/queue.json")
        store = ss.StorageQueueStore(self.storage, key.format(agent_id=self.s.agent_id))
        if self.dry_run:   # read the real decisions, never write the queue
            try:
                store = ss.MemoryQueueStore(store.load())
            except Exception:  # noqa: BLE001
                store = ss.MemoryQueueStore()
        self.queue = ss.AdjudicationQueue(store, cfg=adj, clock=self.c.clock)
        self._load_decisions()

    def _load_decisions(self) -> None:
        assert self.queue is not None
        self.decisions = collections.defaultdict(list)
        try:
            for d in self.queue.decisions():
                self.decisions[d.model_key].append(d)
        except Exception as e:  # noqa: BLE001 - consolidation still runs, undecided
            self.warnings.append(f"adjudication queue unreadable: {type(e).__name__}: {e}"[:200])

    # ------------------------------------------------------------------
    # Phase 1: sources
    # ------------------------------------------------------------------
    def _phase_sources(self, t0: float) -> None:
        cfg = self.s.fetching
        fetch_kinds = set(cfg.get("fetch_kinds") or [])
        priority: dict[str, int] = {}
        if cfg.get("priority_sql") and not self.only_models:
            try:
                for i, mid in enumerate(self.store.priority_model_ids(cfg["priority_sql"])):
                    priority.setdefault(mid, i)
            except Exception as e:  # noqa: BLE001 - priority is advisory
                self.warnings.append(f"sources.priority_sql failed: {type(e).__name__}: {e}"[:200])
            self.store.end_read()
        due: list[tuple[SourceRow, list[str]]] = []
        fetchable = 0
        for src in self.sources:
            model = self.by_key.get(src.model_key)
            if model is None or src.kind not in fetch_kinds or not src.url:
                continue
            eng = self.engines[model.kind]
            slug = self._brand(model, eng, src.url)
            if not self._brand_allowed(slug):
                continue
            fetchable += 1
            reasons = due_reasons(src, now=self.now, refetch_hours=float(cfg["refetch_hours"]),
                                  fingerprint=self._fingerprint(eng, slug))
            if reasons:
                due.append((src, reasons))
        self.metrics["sources_fetchable"] = float(fetchable)
        self.metrics["sources_due"] = float(len(due))
        due = order_due(due, priority=priority)
        cap = int(cfg["max_fetches_per_run"])
        deadline = t0 + float(cfg["max_run_minutes"]) * 60.0
        for i, (src, reasons) in enumerate(due):
            if i >= cap or self.c.monotonic() > deadline:
                self.metrics["sources_left_for_next_run"] = float(len(due) - i)
                self.notes.append(f"{len(due) - i} due source(s) left for the next run "
                                  f"({'fetch cap' if i >= cap else 'time budget'})")
                break
            if i % 10 == 0:
                self.say(f"1/5 sources: fetching {i + 1}/{len(due)}", progress=0.05 + 0.35 * i / max(1, len(due)))
            model = self.by_key[src.model_key]
            eng = self.engines[model.kind]
            res = eng.ss.fetch(src.url, prior=(src.state.get("fetch") or None))
            self.m("sources_attempted")
            self._record_fetch(src, res)

    def _record_fetch(self, src: SourceRow, res: ss.FetchResult, *, count: bool = True) -> None:
        """Record a fetch outcome in the source's state (status, backoff).
        ``count`` False for phase-2 body re-fetches, already counted once."""
        st = src.state
        st["v"] = STATE_VERSION
        st["attempted_at"] = to_iso(self.now)
        st["last_result"] = res.status
        st["reason"] = (res.reason or "")[:300]
        src.dirty = True
        cfg = self.s.fetching
        if res.status in BODY_STATUSES:
            st["fetch"] = res.to_record()
            st["final_url"] = res.final_url
            st["status"] = "ok"
            st["failures"] = 0
            st["next_attempt_at"] = None
            self.fetched[src.key] = res
            if count:
                self.m("sources_changed" if res.status == "fetched" else "sources_unchanged")
            return
        if res.status == "deferred":
            self.m("sources_deferred")
            return
        failures = int(st.get("failures") or 0) + 1
        st["failures"] = failures
        if res.status == "refused":
            st["status"] = "refused"
            st["refused_url"] = src.url
            src.new_candidates = []
            st["candidates_sha"] = candidates_sha([])
            st["next_attempt_at"] = None
            self.m("sources_refused")
            self.decide("warning", f"source {src.id} ({src.url}) refused: {res.reason}; its "
                                   "values were dropped and it will not be fetched again")
            return
        if res.status == "http-error" and int(res.http_status or 0) in GONE_HTTP:
            st["status"] = "gone"
            hours = float(cfg["gone_retry_hours"])
            self.m("sources_gone")
        elif res.status == "robots-disallowed":
            st["status"] = "blocked"
            hours = backoff_hours(failures, float(cfg["retry_base_hours"]), float(cfg["retry_max_hours"]))
            self.m("sources_blocked")
        else:
            st["status"] = "failed"
            hours = backoff_hours(failures, float(cfg["retry_base_hours"]), float(cfg["retry_max_hours"]))
            self.m("sources_failed")
        st["next_attempt_at"] = to_iso(self.now + timedelta(hours=hours))

    # ------------------------------------------------------------------
    # Phase 2: extract (deterministic, then the batched cheap-model pass)
    # ------------------------------------------------------------------
    def _document(self, src: SourceRow, eng: KindEngine, res: Optional[ss.FetchResult]
                  ) -> tuple[Optional[ss.SpecDocument], Optional[ss.FetchResult]]:
        """The source's document, keyed to the source row's URL (redirects
        are recorded in its state, not in provenance). Re-fetches the body
        politely when the fetch cache no longer holds it."""
        if res is None or not res.ok:
            res = eng.ss.fetch(src.url, prior=(src.state.get("fetch") or None), need_body=True)
            self.m("sources_body_requests")
            self._record_fetch(src, res, count=False)
            if not res.ok:
                return None, res
        try:
            doc = eng.ss.document(res, kind=src.kind, publisher=src.publisher)
        except ss.AmazonContentRefused as e:
            self._record_fetch(src, replace(res, status="refused", reason=f"amazon-content: {e}"[:200]))
            return None, res
        except Exception as e:  # noqa: BLE001 - a bad document must not sink the run
            st = src.state
            failures = int(st.get("failures") or 0) + 1
            st.update({"status": "failed", "failures": failures,
                       "reason": f"document: {type(e).__name__}: {e}"[:300],
                       "next_attempt_at": to_iso(self.now + timedelta(hours=backoff_hours(
                           failures, float(self.s.fetching["retry_base_hours"]),
                           float(self.s.fetching["retry_max_hours"]))))})
            src.dirty = True
            self.m("documents_failed")
            self.warnings.append(f"source {src.id}: document failed: {type(e).__name__}: {e}"[:200])
            return None, res
        return replace(doc, url=src.url), res

    def _phase_extract(self) -> None:
        self.say("2/5 extract: deterministic extractors", progress=0.42)
        work: dict[str, list[SourceRow]] = collections.defaultdict(list)
        for src in self.sources:
            res = self.fetched.get(src.key)
            if res is None:
                continue
            model = self.by_key[src.model_key]
            eng = self.engines[model.kind]
            slug = self._brand(model, eng, src.url)
            fp = self._fingerprint(eng, slug)
            prior = {"content_sha256": src.state.get("extracted_sha"),
                     "fingerprint": src.state.get("fingerprint")}
            if self.forced or ss.needs_extraction(prior, content_sha256=res.content_sha256, fingerprint=fp):
                work[src.model_key].append(src)
            else:
                self.m("sources_extraction_skipped")
        # A source re-extracted with the same content keeps its LLM answers
        # (they still quote it): load its stored candidates once, in bulk.
        keep = [s for srcs in work.values() for s in srcs
                if s.candidates is None and s.new_candidates is None and s.state.get("extracted_sha")
                and s.state.get("extracted_sha") == self.fetched[s.key].content_sha256]
        if keep:
            got = self.store.load_candidates([s.id for s in keep])
            for s in keep:
                s.candidates = got.get(s.key, [])
            self.store.end_read()
        llm_models: set[str] = set()
        for key, srcs in self.src_by_model.items():
            model = self.by_key.get(key)
            if self.dry_run or model is None or not self.engines[model.kind].llm_enabled:
                continue
            eng = self.engines[model.kind]
            if any(s.state.get("extracted_sha") and s.state.get("status") != "refused"
                   and (s.state.get("llm") or {}).get("sig") != eng.llm_signature(s.state)
                   for s in srcs) or key in work:
                llm_models.add(key)
        items_by_kind: dict[str, list[ss.LLMItem]] = collections.defaultdict(list)
        item_src: dict[str, SourceRow] = {}
        keys = sorted(set(work) | llm_models, key=lambda k: (k not in work, k))
        for n, key in enumerate(keys):
            model = self.by_key[key]
            eng = self.engines[model.kind]
            docs: dict[str, ss.SpecDocument] = {}
            for src in work.get(key, []):
                prior_sha = src.state.get("extracted_sha")
                doc, res = self._document(src, eng, self.fetched.get(src.key))
                if doc is None:
                    continue
                slug = self._brand(model, eng, src.url)
                ext = eng.ss.extract(doc, brand=slug)
                cands = [cand_to_dict(c) for c in ext.candidates]
                llm_state: dict = {}
                if prior_sha and prior_sha == res.content_sha256:
                    # same content, new extraction config: the LLM answers still
                    # quote this content and stay until a new pass replaces them
                    kept = [d for d in src.current_candidates() if is_llm_candidate(d)]
                    if kept:
                        cands = cands + kept
                        llm_state = dict(src.state.get("llm") or {})
                        self.m("llm_candidates_kept", float(len(kept)))
                src.new_candidates = cands
                src.state.update({"extracted_sha": res.content_sha256, "fingerprint": self._fingerprint(eng, slug),
                                  "extracted_at": to_iso(self.now), "brand": slug,
                                  "errors": list(ext.errors)[:20], "flags": sorted(doc.flags),
                                  "candidates_sha": candidates_sha(cands), "llm": llm_state,
                                  "status": "ok"})
                src.column_update = {"fetched_at": res.fetched_at, "content_sha256": res.content_sha256}
                src.dirty = True
                docs[src.key] = doc
                self.m("sources_extracted")
                self.m("candidates_extracted", float(len(ext.candidates)))
                if ext.errors:
                    self.m("extractor_errors", float(len(ext.errors)))
                    self.warnings.append(f"source {src.id}: {ext.errors[0]}"[:200])
            if key in llm_models:
                self._llm_items(model, eng, docs, items_by_kind, item_src)
            if n % 25 == 0:
                self.say(f"2/5 extract: model {n + 1}/{len(keys)}", progress=0.42 + 0.13 * n / max(1, len(keys)))
        for kind, items in items_by_kind.items():
            self._run_llm(self.engines[kind], items, item_src)
        if not self.dry_run:
            self.store.save_sources(self.sources)

    def _model_candidates(self, model: ModelRow, eng: KindEngine,
                          drop_llm_of: frozenset[str] = frozenset()) -> list[ss.Candidate]:
        """Every candidate of the model's sources plus its operator values.
        ``drop_llm_of``: source keys whose LLM answers are left out (they are
        about to be asked again)."""
        srcs = self.src_by_model.get(model.key, [])
        need = [s.id for s in srcs if s.new_candidates is None and s.candidates is None]
        if need:
            got = self.store.load_candidates(need)
            for s in srcs:
                if s.candidates is None:
                    s.candidates = got.get(s.key, [])
            self.store.end_read()
        cands: list[ss.Candidate] = []
        for s in srcs:
            if s.state.get("status") == "refused":
                continue
            drop = s.key in drop_llm_of
            cands.extend(cand_from_dict(d) for d in s.current_candidates() if not (drop and is_llm_candidate(d)))
        by_id = {s.key: s for s in srcs}
        for row in self.operator_rows.get(model.key, []):
            c = operator_candidate(row, by_id.get(str(row.get("source_id"))), eng.ss.fields)
            if c is not None:
                cands.append(c)
        return cands

    def _llm_items(self, model: ModelRow, eng: KindEngine, docs: dict[str, ss.SpecDocument],
                   items_by_kind: dict[str, list[ss.LLMItem]], item_src: dict[str, SourceRow]) -> None:
        srcs = [s for s in self.src_by_model.get(model.key, [])
                if s.state.get("extracted_sha") and s.state.get("status") != "refused"
                and (s.state.get("llm") or {}).get("sig") != eng.llm_signature(s.state)]
        if not srcs:
            return
        # What is missing WITHOUT these sources' earlier LLM answers: when the
        # LLM config changed, every field they answered is asked again (and
        # replaced only when the new pass completes).
        redo = frozenset(s.key for s in srcs)
        spec = eng.ss.consolidate(model.key, self._model_candidates(model, eng, drop_llm_of=redo),
                                  decisions=self.decisions.get(model.key, []))
        missing = eng.ss.missing_fields(spec)
        llm = eng.ss.llm(agent_id=self.s.agent_id, chat=self.c.chat, admit=self.c.admit)
        for src in srcs:
            sig = eng.llm_signature(src.state)
            if not missing:
                src.state["llm"] = {"sig": sig, "at": to_iso(self.now), "items": 0}
                src.dirty = True
                continue
            doc = docs.get(src.key)
            if doc is None:
                doc, _res = self._document(src, eng, self.fetched.get(src.key))
                if doc is None or doc.content_sha256 != src.state.get("extracted_sha"):
                    continue        # body unavailable or content moved on: next run
            slug = self._brand(model, eng, src.url)
            # A per-row marker on the item's URL (never sent to the model)
            # maps the answers back to this source row, even when two rows
            # share a URL (one manual covering two variants).
            item = llm.build_item(replace(doc, url=src.url + LLM_MARKER + src.key),
                                  model_key=model.key, missing=missing, brand=slug,
                                  model_name=model.name, hints=eng.ss.llm_hints(slug))
            if item is None:
                src.state["llm"] = {"sig": sig, "at": to_iso(self.now), "items": 0}
                src.dirty = True
                continue
            items_by_kind[model.kind].append(item)
            item_src[item.item_id] = src

    def _run_llm(self, eng: KindEngine, items: list[ss.LLMItem], item_src: dict[str, SourceRow]) -> None:
        if not items:
            return
        self.say(f"2/5 extract: cheap-model pass over {len(items)} item(s) ({eng.kind})", progress=0.56)
        llm = eng.ss.llm(agent_id=self.s.agent_id, chat=self.c.chat, admit=self.c.admit)
        res = llm.run(items)
        self.m("llm_items", float(len(items)))
        self.m("llm_calls", float(res.calls))
        self.m("llm_candidates", float(len(res.candidates)))
        self.m("llm_rejected", float(len(res.rejected)))
        self.m("llm_deferred", float(len(res.deferred)))
        if res.reason:
            self.notes.append(f"llm ({eng.kind}): {res.reason}")
        for e in res.errors[:3]:
            self.warnings.append(f"llm ({eng.kind}): {e}"[:200])
        deferred = {it.item_id for it in res.deferred}
        by_marker = {s.key: s for s in item_src.values()}
        fresh: dict[str, list[dict]] = collections.defaultdict(list)
        for c in res.candidates:
            base, sep, marker = c.source.url.rpartition(LLM_MARKER)
            src = by_marker.get(marker) if sep else None
            if src is None or base != src.url:
                continue
            fresh[src.key].append(cand_to_dict(replace(c, source=replace(c.source, url=src.url))))
        for item in items:
            if item.item_id in deferred:
                continue
            src = item_src[item.item_id]
            if src.new_candidates is None and src.candidates is None:
                src.candidates = self.store.load_candidates([src.id]).get(src.key, [])
                self.store.end_read()
            asked = set(item.fields)
            base = [d for d in src.current_candidates()
                    if not (is_llm_candidate(d) and d.get("field") in asked)]
            cands = base + fresh.get(src.key, [])
            src.new_candidates = cands
            src.state["candidates_sha"] = candidates_sha(cands)
            src.state["llm"] = {"sig": eng.llm_signature(src.state), "at": to_iso(self.now),
                                "items": 1, "candidates": len(fresh.get(src.key, []))}
            src.dirty = True

    # ------------------------------------------------------------------
    # Phase 3: validate (adjudicate, consolidate, write, queue)
    # ------------------------------------------------------------------
    def _phase_validate(self) -> None:
        assert self.queue is not None
        self.say("3/5 validate: adjudication", progress=0.6)
        adj_cfg = self.common.get("adjudication") or {}
        if adj_cfg.get("enabled") and not self.dry_run:
            fields: dict[str, Any] = {}
            for eng in self.engines.values():
                for name, spec in (eng.cfg.get("fields") or {}).items():
                    fields.setdefault(name, spec)
            try:
                adjudicator = ss.Adjudicator(adj_cfg, fields=ss.FieldRegistry(fields),
                                             agent_id=self.s.agent_id, chat=self.c.chat,
                                             admit=self.c.admit)
                ar = adjudicator.run(self.queue)
                self.m("adjudication_decisions", float(len(ar.decisions)))
                self.m("adjudication_spent_s", float(ar.spent_s))
                if ar.reason:
                    self.notes.append(f"adjudication: {ar.reason}")
                for e in ar.errors[:2]:
                    self.warnings.append(f"adjudication: {e}"[:200])
            except Exception as e:  # noqa: BLE001 - a busy queue is retried next run
                self.warnings.append(f"adjudication skipped: {type(e).__name__}: {e}"[:200])
            self._load_decisions()

        self.say("3/5 validate: consolidating changed models", progress=0.62)
        hashes: dict[str, str] = {}
        cfg_fp = {k: e.consolidation_fp(self.s) for k, e in self.engines.items()}
        for model in self.models:
            srcs = self.src_by_model.get(model.key, [])
            # candidates_sha covers each candidate's SourceRef (fetch time and
            # content hash included), so the timestamp columns are not hashed
            # again (their text form differs between a fresh fetch and a DB read).
            hashes[model.key] = stable_hash({
                "kind": model.kind,
                "sources": sorted([[s.key, s.url, s.kind, s.publisher, s.state.get("status") == "refused",
                                    s.state.get("candidates_sha") or ""] for s in srcs]),
                "manual": sorted([[str(r.get("id")), r.get("field"), num(r.get("value_num")),
                                   r.get("value_text"), r.get("unit"), str(r.get("source_id")),
                                   num(r.get("confidence"))] for r in self.operator_rows.get(model.key, [])],
                                 key=repr),
                "decisions": sorted([[d.conflict_id, d.choice, d.decided_by]
                                     for d in self.decisions.get(model.key, [])], key=repr),
                "cfg": cfg_fp[model.kind],
                "engine": ENGINE_VERSION,
            }, float_precision=6)
        prior_hashes = dict(self.prior.get("model_hashes") or {})
        prior_counts = {k: int(v) for k, v in (self.prior.get("model_revisits") or {}).items()}
        revisit = int(self.s.block["consolidate"]["revisit_after_runs"])
        fresh, cached, counters = partition_by_hash(
            self.models, prior_hashes, key_fn=lambda m: m.key, hash_fn=lambda m: hashes[m.key],
            revisit_counter=prior_counts, revisit_after_runs=0 if self.forced else revisit)
        changed_first = sorted(fresh, key=lambda m: (prior_hashes.get(m.key) == hashes[m.key], str(m.id)))
        cap = int(self.s.block["consolidate"]["max_models_per_run"])
        todo, left = changed_first[:cap], changed_first[cap:]
        self.metrics["models_unchanged"] = float(len(cached))
        self.metrics["models_to_consolidate"] = float(len(fresh))
        new_hashes = {m.key: hashes[m.key] for m in cached}
        new_counts = {m.key: counters.get(m.key, 0) for m in cached}
        for m in left:
            if m.key in prior_hashes:
                new_hashes[m.key] = prior_hashes[m.key]
                new_counts[m.key] = prior_counts.get(m.key, revisit)
        if left:
            self.notes.append(f"{len(left)} model(s) left for the next run (max_models_per_run)")

        open_items = self._open_queue_items()
        all_conflicts: list[ss.Conflict] = []
        batch = int(self.s.block["consolidate"]["batch_models"])
        for i in range(0, len(todo), batch):
            chunk = todo[i:i + batch]
            existing = {} if self.dry_run else self.store.load_engine_values([m.id for m in chunk])
            self.store.end_read()
            for model in chunk:
                try:
                    spec = self._consolidate_and_write(model, existing.get(model.key, []))
                except Exception as e:  # noqa: BLE001 - one bad row must not block the catalog
                    self.store.end_read()
                    self.m("models_write_failed")
                    if self.metrics["models_write_failed"] <= 5:
                        self.problems.append(f"model {model.id}: write failed: {type(e).__name__}: {e}"[:300])
                    if model.key in prior_hashes:     # not recorded as done: retried next run
                        new_hashes[model.key] = prior_hashes[model.key]
                        new_counts[model.key] = revisit
                    continue
                all_conflicts.extend(spec.conflicts)
                active = {c.conflict_id for c in spec.conflicts}
                if not self.dry_run and (open_items.get(model.key, set()) - active):
                    try:
                        self.metrics["conflicts_retired"] += float(self.queue.retire(model.key, active))
                    except Exception as e:  # noqa: BLE001
                        self.warnings.append(f"queue retire failed: {type(e).__name__}: {e}"[:200])
                new_hashes[model.key] = hashes[model.key]
                new_counts[model.key] = 0
            self.say(f"3/5 validate: {min(i + batch, len(todo))}/{len(todo)} model(s)",
                     progress=0.62 + 0.18 * (i + batch) / max(1, len(todo)))
        if all_conflicts and not self.dry_run:
            try:
                self.metrics["conflicts_enqueued"] = float(self.queue.enqueue(all_conflicts))
            except Exception as e:  # noqa: BLE001 - the next run enqueues them again
                self.warnings.append(f"queue enqueue failed: {type(e).__name__}: {e}"[:200])
        if not self.dry_run and self.only_models is None:
            # Conflicts of models that left the catalog (retired, kind no
            # longer configured) must not keep spending adjudication budget.
            for key in sorted(k for k in open_items if k and k not in self.by_key):
                try:
                    self.metrics["conflicts_retired"] += float(self.queue.retire(key, set()))
                except Exception as e:  # noqa: BLE001
                    self.warnings.append(f"queue retire failed: {type(e).__name__}: {e}"[:200])
        self.state["model_hashes"] = new_hashes
        self.state["model_revisits"] = new_counts

    def _open_queue_items(self) -> dict[str, set[str]]:
        assert self.queue is not None
        try:
            doc = self.queue.store.load() or {}
        except Exception:  # noqa: BLE001
            return {}
        out: dict[str, set[str]] = collections.defaultdict(set)
        for cid, item in (doc.get("items") or {}).items():
            if item.get("status") in ("pending", "needs-human", "dropped-over-cap"):
                out[str((item.get("conflict") or {}).get("model_key", ""))].add(cid)
        return out

    def _consolidate_and_write(self, model: ModelRow, existing: Sequence[Mapping[str, Any]]) -> ss.SpecSet:
        eng = self.engines[model.kind]
        cands = self._model_candidates(model, eng)
        spec = eng.ss.consolidate(model.key, cands, decisions=self.decisions.get(model.key, []))
        srcs = self.src_by_model.get(model.key, [])
        source_ids = {s.url: s.id for s in srcs}
        rows, operator_status, dropped = engine_value_rows(spec, model, source_ids)
        if dropped:
            self.m("values_dropped", float(dropped))
        status = model_status(spec, bool(srcs or self.operator_rows.get(model.key)))
        if self.dry_run:
            self.m("models_consolidated")
            self.m(f"models_{status}")
            self.notes.append(f"[dry-run] model {model.id} {model.name}: {status} "
                              f"{round(spec.completeness * 100)}% {spec.summary()['values']}"[:600])
            return spec
        typed = (eng.settings, spec.values(), spec.completeness) if eng.settings.typed else None
        others = [e.settings for k, e in self.engines.items() if k != model.kind and e.settings.typed]
        stats = self.store.write_model(model, rows, existing, operator_status,
                                       self.operator_rows.get(model.key, []), typed=typed,
                                       status=(status, spec.completeness), other_typed=others)
        self.m("models_consolidated")
        self.m(f"models_{status}")
        if stats["values_inserted"] or stats["values_deleted"] or stats["typed_upserts"] \
                or stats["typed_deletes"] or stats["operator_status_updates"]:
            self.m("models_written")
        for k, v in stats.items():
            self.m(k, float(v))
        return spec

    # ------------------------------------------------------------------
    # Phase 4: compat
    # ------------------------------------------------------------------
    def _phase_compat(self) -> bool:
        cfg = self.s.compat
        prior = dict(self.prior.get("compat") or {})
        if not cfg.get("enabled") or self.dry_run:
            if prior:
                self.state["compat"] = prior
            return True
        self.say("4/5 compat: loading the graph inputs", progress=0.82)
        try:
            if self.c.compat_file_cfg is not None:
                file_cfg = dict(self.c.compat_file_cfg)
            else:
                file_cfg = cg.load_file_config(self.storage)
            gcfg = cg.resolve_config(site=self.s.site, agent_id=self.s.agent_id,
                                     site_cfg=dict(cfg.get("graph") or {}), file_cfg=file_cfg)
            connectors = self.store.connectors()
            graph = cg.CompatGraph(gcfg, connectors=connectors, clock=lambda: to_iso(self.now))
        except Exception as e:  # noqa: BLE001
            self.problems.append(f"compat: cannot build the graph: {type(e).__name__}: {e}"[:300])
            self.state["compat"] = prior
            return False
        if graph.problems:
            self.problems += [f"compat: {p}" for p in graph.problems]
            self.state["compat"] = prior
            return False
        phase = CompatPhase(self, graph, connectors)
        try:
            self.state["compat"] = phase.run(prior)
        except Exception as e:  # noqa: BLE001 - keep the prior state: the next run retries
            self.problems.append(f"compat failed: {type(e).__name__}: {e}"[:300])
            failed = dict(prior)
            if phase.owned is not None:
                # the engine-written manual rows as committed so far (batches
                # before the failure may have released or written some)
                failed["owned_manual"] = dict(phase.owned)
            self.state["compat"] = failed
            try:
                self.store.conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            return False
        return True

    # ------------------------------------------------------------------
    # Phase 5: runtime
    # ------------------------------------------------------------------
    def _phase_runtime(self) -> None:
        rt = self.s.runtime
        if not rt.get("enabled") or self.dry_run:
            return
        self.say("5/5 runtime: estimates", progress=0.92)
        est = RuntimeEstimator(rt)
        kinds = set(rt.get("kinds") or [])
        models = [m for m in self.store.load_models(kinds)]
        if self.only_models is not None:
            models = [m for m in models if m.key in self.only_models]
        required = {k: set(self.engines[k].required) for k in kinds if k in self.engines}
        need_fields = set(est.fields) | set().union(*required.values()) if required else set(est.fields)
        rows = self.store.accepted_rows([m.id for m in models], fields=need_fields)
        presets = self.store.load_presets()
        live = self.store.runtime_model_ids()
        live_ids = {str(x) for x in live}
        self.store.end_read()
        by_model: dict[str, list[dict]] = collections.defaultdict(list)
        for r in rows:
            by_model[str(r["model_id"])].append(r)
        desired: dict[str, list[dict]] = {}
        for m in models:
            node = cg.ModelSpec.from_db_rows(m.id, m.kind, by_model.get(m.key, []))
            if rt.get("require_complete", True):
                req = required.get(m.kind, set())
                if not all(node.get(f) is not None for f in req):
                    continue
            out = est.rows(m.id, node.values, presets)
            if out:
                desired[m.key] = out
        existing = self.store.load_runtime_rows([m.id for m in models if m.key in desired or m.key in live_ids])
        self.store.end_read()
        changes: dict[Any, list[dict]] = {}
        for m in models:
            want = sorted(_row_tuple(r) for r in desired.get(m.key, []))
            have = sorted(_row_tuple(r) for r in existing.get(m.key, []))
            if want != have:
                changes[m.id] = desired.get(m.key, [])
        if self.only_models is None:
            # Models that left the scope (retired, another kind, a kind no
            # longer estimated) lose their estimates: the table is a
            # projection of the current catalog, and the goal counts it.
            in_scope = {m.key for m in models}
            gone = [mid for mid in live if str(mid) not in in_scope]
            for mid in gone:
                changes[mid] = []
            if gone:
                self.metrics["runtime_models_removed"] = float(len(gone))
        stats = self.store.replace_runtime_rows(changes)
        for k, v in stats.items():
            self.m(k, float(v))
        self.metrics["runtime_models_estimated"] = float(len(desired))
        self.metrics["runtime_presets"] = float(len(presets))

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------
    def _final_metrics(self) -> None:
        if self.dry_run:
            return
        ids = [m.id for m in self.models]
        req_fields = set()
        for e in self.engines.values():
            req_fields |= set(e.required)
        acc: dict[str, set[str]] = collections.defaultdict(set)
        for r in self.store.accepted_rows(ids, fields=req_fields):
            acc[str(r["model_id"])].add(str(r["field"]))
        def has_inputs(m: ModelRow) -> bool:
            return bool(self.src_by_model.get(m.key) or self.operator_rows.get(m.key))

        # A kind without required fields would be "complete" with nothing at
        # all: complete also needs a source or an operator value.
        complete = {m.key for m in self.models
                    if has_inputs(m) and set(self.engines[m.kind].required) <= acc.get(m.key, set())}
        conflicted = self.store.open_conflicts(ids)
        conflict_fields: dict[str, set[str]] = collections.defaultdict(set)
        for mid, fld in conflicted:
            conflict_fields[mid].add(fld)
        for m in self.models:
            req = set(self.engines[m.kind].required)
            if m.key in complete:
                state = "complete"
            elif req & conflict_fields.get(m.key, set()):
                state = "conflict"
            elif req & acc.get(m.key, set()):
                state = "partial"
            else:
                state = "missing"
            self.metrics[f"catalog_models_{state}"] += 1.0
        primary = set((self.s.block.get("metrics") or {}).get("primary_kinds") or []) or set(self.engines)
        prim_models = [m for m in self.models if m.kind in primary]
        self.metrics["primary_models_total"] = float(len(prim_models))
        self.metrics["primary_models_complete"] = float(sum(1 for m in prim_models if m.key in complete))
        self.metrics["models_spec_complete"] = float(len(complete))
        products = self.store.load_products() if self.s.products is not None else []
        if products and self.only_models is None:
            kinds = {m.key: m.kind for m in self.store.load_models(primary | set(self.engines))}
            prim = [p for p in products if (p.get("kind") or kinds.get(str(p.get("model_id")))) in primary]
            done = sum(1 for p in prim if str(p.get("model_id")) in complete)
            self.metrics["primary_products_total"] = float(len(prim))
            self.metrics["primary_products_complete"] = float(done)
            self.metrics["primary_spec_complete_pct"] = round(100.0 * done / len(prim), 2) if prim else 0.0
        else:
            n = len(prim_models)
            self.metrics["primary_spec_complete_pct"] = (
                round(100.0 * self.metrics["primary_models_complete"] / n, 2) if n else 0.0)
        self.metrics["open_spec_conflicts"] = float(len(conflicted))
        amazon = {s.key for s in self.sources if ss.is_amazon_url(s.url)}
        counts = self.store.value_counts_by_source(ids)
        self.metrics["amazon_sourced_values"] = float(sum(n for sid, n in counts.items() if sid in amazon))
        fetchable_states = [s for s in self.sources if s.kind in set(self.s.fetching.get("fetch_kinds") or [])]
        unhealthy = sum(1 for s in fetchable_states if s.state.get("status") in UNHEALTHY)
        self.metrics["sources_unhealthy"] = float(unhealthy)
        self.metrics["sources_unhealthy_pct"] = (round(100.0 * unhealthy / len(fetchable_states), 2)
                                                 if fetchable_states else 0.0)
        att = self.metrics.get("sources_attempted", 0.0)
        bad = sum(self.metrics.get(k, 0.0) for k in ("sources_failed", "sources_gone", "sources_blocked",
                                                     "sources_refused"))
        self.metrics["source_fetch_failed_pct"] = round(100.0 * bad / att, 2) if att else 0.0
        if self.s.runtime.get("enabled"):
            rt_kinds = set(self.s.runtime.get("kinds") or [])
            self.metrics["runtime_models_live"] = float(self.store.runtime_models_live(
                [m.id for m in self.models if m.kind in rt_kinds]))
        try:
            doc = self.queue.store.load() if self.queue is not None else {}
        except Exception:  # noqa: BLE001
            doc = {}
        items = (doc or {}).get("items") or {}
        self.metrics["conflicts_queue_pending"] = float(sum(1 for v in items.values() if v.get("status") == "pending"))
        self.metrics["conflicts_needs_human"] = float(sum(1 for v in items.values()
                                                          if v.get("status") == "needs-human"))
        self.store.end_read()

    def _summary(self, rep: RunReport) -> str:
        g = self.metrics.get
        if rep.status == "failure" and self.problems:
            head = f"FAILED: {self.problems[0]}"[:200] + " | "
        else:
            head = "short-circuited: no source or model changed | " if rep.short_circuited else ""
        parts = [f"{int(g('sources_attempted', 0))}/{int(g('sources_due', 0))} due source(s) fetched "
                 f"({int(g('sources_changed', 0))} changed, {int(g('sources_extracted', 0))} extracted)",
                 f"{int(g('models_consolidated', 0))} model(s) consolidated, {int(g('models_written', 0))} written",
                 f"{g('primary_spec_complete_pct', 0):g}% primary spec-complete",
                 f"{int(g('open_spec_conflicts', 0))} open conflict(s)"]
        if self.s.compat.get("enabled"):
            parts.append(f"compat {int(g('compat_upserts', 0))} upsert(s) / {int(g('compat_deletes', 0))} delete(s)")
        if self.s.runtime.get("enabled"):
            parts.append(f"{int(g('runtime_models_live', 0))} model(s) with runtime estimates")
        return (head + "; ".join(parts))[:500]

    def _summary_md(self, rep: RunReport) -> str:
        lines = [f"## spec-extractor run ({rep.status})", "", rep.summary, ""]
        if self.problems:
            lines += ["### Problems", *[f"- {p}" for p in self.problems[:20]], ""]
        if self.notes:
            lines += ["### Notes", *[f"- {n}" for n in self.notes[:30]], ""]
        if self.warnings:
            lines += ["### Warnings", *[f"- {w}" for w in self.warnings[:30]], ""]
        lines += ["### Metrics", *[f"- {k}: {v:g}" if isinstance(v, (int, float)) else f"- {k}: {v}"
                                   for k, v in sorted(self.metrics.items())]]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Compat phase
# ---------------------------------------------------------------------------

#: compat_edges key fields (framework.core.compat_graph's row key).
EDGE_KEY_FIELDS = ("a_model_id", "b_model_id", "relation", "rule_id")


def _idk(x: Any) -> str:
    """A model id as text (DB ints and fixture strings compare equal)."""
    return "" if x is None else str(x)


def edge_key(a: Any, b: Any, relation: Any, rule_id: Any) -> str:
    """The state key of one compat_edges row (JSON: an id may hold any character)."""
    return json.dumps([_idk(a), _idk(b), str(relation or ""), str(rule_id or "")])


def _split_edge_key(key: str) -> Optional[tuple[str, str, str, str]]:
    try:
        a, b, rel, rid = json.loads(key)
    except (TypeError, ValueError):
        return None
    return str(a), str(b), str(rel), str(rid)


def owned_sig(verdict: Any, via: Any, rule_version: Any, inputs_hash: Any) -> str:
    """The content of a manual-confidence row the engine wrote. A row that
    still hashes to the recorded value is the engine's own: nobody edited it."""
    return stable_hash([str(verdict or ""), _idk(via), str(rule_version or ""), str(inputs_hash or "")],
                       digest_chars=16)


def _row_inputs_hash(row: Mapping[str, Any]) -> str:
    if "inputs_hash" in row:
        return str(row.get("inputs_hash") or "")
    cfg = row.get("config")
    if isinstance(cfg, str):
        try:
            cfg = json.loads(cfg)
        except ValueError:
            cfg = {}
    return str(cfg.get("inputs_hash") or "") if isinstance(cfg, dict) else ""


def row_owned_sig(row: Mapping[str, Any]) -> str:
    return owned_sig(row.get("verdict"), row.get("via_model_id"), row.get("rule_version"),
                     _row_inputs_hash(row))


def edge_owned_sig(edge: cg.Edge) -> str:
    return owned_sig(edge.verdict, edge.via_model_id, edge.rule_version, edge.inputs_hash)


class CompatPhase:
    """compat_edges from accepted spec values, incremental by changed nodes.

    State: {graph_sig, node_sigs {model id: sig}, record_sigs {pair: sig},
    runs_since_full, owned_manual {edge key: sig}}. A full rebuild (plus
    deleting rows of A-models that left the catalog) runs on the first run, a
    config change, stale rule versions in the table, and every
    ``full_rebuild_every_runs`` runs. Otherwise only pairs touching changed
    models are rebuilt: a model whose node changed, and both ends of every
    fit record that appeared, changed or was withdrawn (a published list
    edited, a fit-records row deleted). Both builds are planned with
    ``scope="full"``: a partial build deletes only rows inside the part it
    covered (``GraphResult.covers``), so a pair that no longer yields a
    stored edge loses its row on the next run even when it was not evaluated
    at all (a ``manufacturer_list``-only ruleset evaluates only pairs that
    still have a record). Rows of removed models are deleted.

    Manual rows. compat_graph protects every ``confidence = 'manual'`` row,
    in the plan and in SQL. The engine writes such rows itself from fit
    records of origin ``manual``, so it records each one it wrote
    (``owned_manual``: key -> :func:`owned_sig` of verdict, via, rule
    version and inputs hash) and releases it once it no longer derives that
    row (the record was withdrawn, a model left the catalog, the ruleset was
    disabled): a guarded DELETE in the same transaction as the plan, after
    which the edge that now decides the pair (if any) is written. A manual
    row the engine did not write, or whose content changed since it wrote it
    (an operator edited it), is never touched. With the state lost, the next
    (full) build adopts every manual row it derives identically again."""

    def __init__(self, run: SpecExtractorRun, graph: cg.CompatGraph, connectors: list[dict]):
        self.r = run
        self.g = graph
        self.connectors = connectors
        self.cfg = run.s.compat
        self.writer = cg.EdgeWriter(run.store.conn, cfg=graph.cfg)
        #: Engine-written manual rows (None until run() loads the prior state;
        #: always consistent with what is committed, so a failing run keeps it).
        self.owned: Optional[dict[str, str]] = None

    def _kinds(self) -> tuple[set[str], set[str]]:
        a, b = set(), set()
        for rs in self.g.rulesets.values():
            if rs.enabled:
                a |= set(rs.a_kinds)
                b |= set(rs.b_kinds)
        for fl in self.cfg.get("fit_lists") or []:
            a |= set(fl.get("kinds") or [])
            b |= set(fl.get("list_kinds") or [])
        return a, b

    def nodes(self) -> list[cg.ModelSpec]:
        a, b = self._kinds()
        models = self.r.store.load_models(a | b)
        rows = self.r.store.accepted_rows([m.id for m in models])
        self.r.store.end_read()
        by_model: dict[str, list[dict]] = collections.defaultdict(list)
        for row in rows:
            by_model[str(row["model_id"])].append(row)
        out = []
        for m in models:
            extra = {}
            if m.model_number and not any(r.get("field") == "model_number" for r in by_model.get(m.key, [])):
                extra["model_number"] = m.model_number
            out.append(cg.ModelSpec.from_db_rows(m.id, m.kind, by_model.get(m.key, []), name=m.name,
                                                 brand=m.brand, aliases=m.aliases, extra=extra or None))
        return out

    def records(self, nodes: list[cg.ModelSpec]) -> tuple[list[cg.FitRecord], int]:
        recs: list[cg.FitRecord] = []
        unresolved = 0
        lists = self.cfg.get("fit_lists") or []
        if lists:
            index = cg.ModelIndex(nodes, number_fields=tuple(self.cfg.get("number_fields") or ("model_number",)))
            for fl in lists:
                kinds = set(fl.get("kinds") or [])
                for n in nodes:
                    if kinds and n.kind not in kinds:
                        continue
                    fv = n.get(str(fl["field"]))
                    if fv is None or not isinstance(fv.value, str):
                        continue
                    got, bad = cg.fit_records_from_list(
                        n.model_id, fv.value, index=index, relation=str(fl["relation"]),
                        subject_side=str(fl.get("subject_side") or "b"),
                        verdict=str(fl.get("verdict") or cg.FITS), brand=n.brand,
                        kinds=fl.get("list_kinds") or None, source_url=fv.source_url,
                        source_kind=fv.source_kind, publisher=fv.publisher,
                        content_sha256=fv.content_sha256, source_id=fv.source_id)
                    recs += got
                    unresolved += len(bad)
        for row in self.r.store.fit_records():
            d = {k: v for k, v in row.items() if v is not None}
            if isinstance(d.get("config"), str):
                try:
                    d["config"] = json.loads(d["config"])
                except ValueError:
                    d["config"] = {}
            recs.append(cg.FitRecord.from_dict(d))
        self.r.store.end_read()
        return recs, unresolved

    def graph_sig(self) -> str:
        """Everything that changes every edge: the resolved graph config, the
        connector table, the primitive's version and this phase's purge
        policy (a change of any of them triggers a full rebuild)."""
        cfg = {k: v for k, v in self.g.cfg.items() if k not in ("site", "agent_id", "config_problems")}
        return stable_hash({"cfg": cfg, "connectors": self.connectors, "engine": cg.ENGINE_VERSION,
                            "purge_orphaned": str(self.cfg.get("purge_orphaned") or "disabled")},
                           float_precision=9)

    # -- SQL plumbing ------------------------------------------------------
    def _select(self, sql: str, params: Mapping[str, Any]) -> list[dict]:
        with self.r.store.conn.cursor() as cur:
            cur.execute(sql, dict(params))
            names = [d[0] for d in cur.description]
            rows = [dict(zip(names, r)) for r in cur.fetchall()]
        self.r.store.end_read()
        return rows

    def _row_sql(self, where: str) -> str:
        """Key, confidence and the owned-row signature fields of matching rows
        (the inputs hash only, not the whole config)."""
        c = self.writer.cols
        return (f"SELECT {c['a_model_id']} AS a_model_id, {c['b_model_id']} AS b_model_id, "
                f"{c['relation']} AS relation, {c['rule_id']} AS rule_id, "
                f"{c['confidence']} AS confidence, {c['verdict']} AS verdict, "
                f"{c['via_model_id']} AS via_model_id, {c['rule_version']} AS rule_version, "
                f"{c['config']}->>'inputs_hash' AS inputs_hash FROM {self.writer.table} WHERE {where}")

    def _existing(self, a_ids: Sequence[Any], rule_ids: Sequence[str]) -> list[dict]:
        if not a_ids:
            return []
        sql = self.writer.select_sql() + f" AND {self.writer.cols['a_model_id']} = ANY(%(a_ids)s)"
        return self._select(sql, {"all": False, "rule_ids": sorted(rule_ids), "a_ids": list(a_ids)})

    def _stale_versions(self) -> bool:
        c = self.writer.cols
        sql = (f"SELECT DISTINCT {c['rule_id']} AS rule_id, {c['rule_version']} AS rule_version "
               f"FROM {self.writer.table} WHERE {c['confidence']} IS DISTINCT FROM '{cg.CONF_MANUAL}'")
        rows = self._select(sql, {})
        return any(str(x["rule_id"]) in self.g.rulesets and self.g.rulesets[str(x["rule_id"])].enabled
                   and str(x["rule_version"]) != self.g.rulesets[str(x["rule_id"])].composite_version
                   for x in rows)

    # -- engine-owned manual rows -------------------------------------------
    def _owned(self) -> dict[str, str]:
        if self.owned is None:
            self.owned = {}
        return self.owned

    def _owns(self, row: Mapping[str, Any]) -> bool:
        """True for a manual row the engine wrote and nobody changed since."""
        key = edge_key(*(row.get(k) for k in EDGE_KEY_FIELDS))
        sig = self._owned().get(key)
        return sig is not None and sig == row_owned_sig(row)

    def _forget(self, pred: Callable[[str, str, str, str], bool]) -> None:
        owned = self._owned()
        for key in list(owned):
            parts = _split_edge_key(key)
            if parts is None or pred(*parts):
                owned.pop(key, None)

    def _release_sql(self) -> str:
        """Deletes one manual row only while it holds what the engine wrote:
        an operator edit in the meantime makes it a no-op."""
        c = self.writer.cols
        return (f"DELETE FROM {self.writer.table} WHERE {c['a_model_id']} = %(a_model_id)s "
                f"AND {c['b_model_id']} = %(b_model_id)s AND {c['relation']} = %(relation)s "
                f"AND {c['rule_id']} = %(rule_id)s AND {c['confidence']} = '{cg.CONF_MANUAL}' "
                f"AND {c['verdict']} = %(verdict)s AND {c['rule_version']} = %(rule_version)s "
                f"AND {c['via_model_id']} IS NOT DISTINCT FROM %(via_model_id)s "
                f"AND COALESCE({c['config']}->>'inputs_hash', '') = %(inputs_hash)s")

    def _apply(self, plan: cg.EdgePlan, releases: Sequence[Mapping[str, Any]]) -> tuple[dict, int]:
        """One transaction: release the engine's own manual rows (guarded
        DELETEs), then apply the plan (which may write the edge that now
        decides a released pair). Returns (writer counts, rows released)."""
        conn = self.writer.conn
        released = 0
        try:
            if releases:
                sql = self._release_sql()
                with conn.cursor() as cur:
                    for row in releases:
                        params = {k: row.get(k) for k in (*EDGE_KEY_FIELDS, "verdict", "rule_version",
                                                          "via_model_id")}
                        params["inputs_hash"] = _row_inputs_hash(row)
                        cur.execute(sql, params)
                        released += max(0, int(cur.rowcount or 0))
            applied = self.writer.apply(plan, commit=False)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        owned = self._owned()
        for row in releases:    # deleted, or edited / gone meanwhile: either way no longer ours
            owned.pop(edge_key(*(row.get(k) for k in EDGE_KEY_FIELDS)), None)
        if released:
            self.r.m("compat_manual_released", float(released))
        return applied, released

    def _delete_keys(self, where: str, params: Mapping[str, Any], rulesets: Sequence[str]) -> int:
        """Delete the engine's rows matching ``where`` among ``rulesets``:
        non-manual rows, and manual rows it wrote itself (unchanged since).
        Other manual rows stay. Returns the number of rows deleted."""
        c = self.writer.cols
        rows = self._select(self._row_sql(f"{c['rule_id']} = ANY(%(rule_ids)s) AND ({where})"),
                            dict(params, rule_ids=sorted(rulesets)))
        keys = [{k: row[k] for k in EDGE_KEY_FIELDS} for row in rows
                if str(row.get("confidence") or "") != cg.CONF_MANUAL]
        releases = [row for row in rows if str(row.get("confidence") or "") == cg.CONF_MANUAL
                    and self._owns(row)]
        if not keys and not releases:
            return 0
        applied, released = self._apply(cg.EdgePlan(deletes=keys), releases)
        return int(applied["deleted"]) + released

    def _purge_orphans(self, active: Sequence[str]) -> None:
        """Rows of rulesets that are not active. A ruleset the profile has
        DISABLED is the operator withdrawing its verdicts (the kill switch for
        an unsafe rule), so the engine's rows of it are deleted
        (``purge_orphaned: disabled``, the default); rows of rulesets the
        profile does not know are deleted only with ``all`` (another config
        layer may own them) and are otherwise counted and reported. The
        engine's rows are its non-manual rows and the manual rows it wrote
        itself; other manual rows are never touched."""
        r = self.r
        mode = str(self.cfg.get("purge_orphaned") or "disabled")
        disabled = {rs.id for rs in self.g.rulesets.values() if not rs.enabled}
        c = self.writer.cols
        rows = self._select(self._row_sql(f"NOT ({c['rule_id']} = ANY(%(active)s))"), {"active": sorted(active)})
        keys: list[dict] = []
        releases: list[dict] = []
        still_owned: set[str] = set()
        kept: collections.Counter = collections.Counter()
        for row in rows:
            rid = str(row.get("rule_id") or "")
            manual = str(row.get("confidence") or "") == cg.CONF_MANUAL
            purge = (rid in disabled and mode in ("disabled", "all")) or (rid not in disabled and mode == "all")
            if manual and not self._owns(row):
                kept["manual"] += 1
                continue
            if manual:
                still_owned.add(edge_key(*(row.get(k) for k in EDGE_KEY_FIELDS)))
            if not purge:
                kept["disabled" if rid in disabled else "unknown"] += 1
            elif manual:
                releases.append(row)
            else:
                keys.append({k: row[k] for k in EDGE_KEY_FIELDS})
        r.metrics["compat_orphaned_rows"] = float(len(rows))
        if keys or releases:
            applied, released = self._apply(cg.EdgePlan(deletes=keys), releases)
            n = float(int(applied["deleted"]) + released)
            r.m("compat_purged", n)
            r.m("compat_deletes", n)
            r.notes.append(f"compat: deleted {int(n)} row(s) of disabled or removed rulesets")
        active_set = set(active)
        # owned rows of inactive rulesets that are gone or were edited are no longer ours
        self._forget(lambda a, b, rel, rid: rid not in active_set and edge_key(a, b, rel, rid) not in still_owned)
        if kept.get("unknown") or kept.get("disabled"):
            r.notes.append(f"compat: kept {kept.get('unknown', 0) + kept.get('disabled', 0)} row(s) of "
                           f"rulesets that are not active (compat.purge_orphaned: {mode})")

    def run(self, prior: Mapping[str, Any]) -> dict:
        r = self.r
        self.owned = {str(k): str(v) for k, v in dict(prior.get("owned_manual") or {}).items()}
        nodes = self.nodes()
        records, unresolved = self.records(nodes)
        r.metrics["compat_nodes"] = float(len(nodes))
        r.metrics["compat_fit_records"] = float(len(records))
        r.metrics["compat_fit_names_unresolved"] = float(unresolved)
        gsig = self.graph_sig()
        node_sigs = {str(n.model_id): stable_hash(n.to_dict(), float_precision=6) for n in nodes}
        rec_sigs: dict[str, list[str]] = collections.defaultdict(list)
        for rec in records:
            key = "|".join(sorted([str(rec.a), str(rec.b)])) + "|" + rec.relation
            rec_sigs[key].append(stable_hash(rec.signature(), float_precision=6))
        rec_sig = {k: stable_hash(sorted(v)) for k, v in rec_sigs.items()}
        runs_since_full = int(prior.get("runs_since_full", 10 ** 6)) + 1
        every = int(self.cfg.get("full_rebuild_every_runs", 14))
        active = [rs.id for rs in self.g.rulesets.values() if rs.enabled]
        active_set = set(active)
        full_reason = ""
        if prior.get("graph_sig") != gsig:
            # (a failed first run leaves only owned_manual in the state)
            full_reason = "first run" if not prior.get("graph_sig") else "graph config changed"
        elif r.forced:
            full_reason = "forced run (AGENT_FORCE_RUN)"
        elif runs_since_full >= every:
            full_reason = f"periodic ({every} runs)"
        elif self._stale_versions():
            full_reason = "stale rule versions"
        a_kinds = {k for rs in self.g.rulesets.values() if rs.enabled for k in rs.a_kinds}
        b_kinds = {k for rs in self.g.rulesets.values() if rs.enabled for k in rs.b_kinds}
        a_ids = [n.model_id for n in nodes if n.kind in a_kinds]
        new_state = {"graph_sig": gsig, "node_sigs": node_sigs, "record_sigs": rec_sig,
                     "runs_since_full": 0 if full_reason else runs_since_full,
                     "owned_manual": self.owned}
        prior_nodes = dict(prior.get("node_sigs") or {})
        removed = sorted(set(prior_nodes) - set(node_sigs))
        if full_reason:
            r.metrics["compat_mode_full"] = 1.0
            r.notes.append(f"compat: full rebuild ({full_reason})")
            self._build(nodes, records, a_ids, changed=None, rulesets=active)
            keep = {_idk(x) for x in a_ids}
            if a_ids:
                where = f"NOT ({self.writer.cols['a_model_id']} = ANY(%(keep)s))"
                r.m("compat_deletes", float(self._delete_keys(where, {"keep": list(a_ids)}, active)))
            else:
                r.m("compat_deletes", float(self._delete_keys("TRUE", {}, active)))
            # every owned row of an active ruleset whose A side is not an A
            # model any more was just deleted (or is gone / edited): forget it
            self._forget(lambda a, b, rel, rid: rid in active_set and a not in keep)
            self._purge_orphans(active)
            return new_state
        changed = {k for k, v in node_sigs.items() if prior_nodes.get(k) != v}
        prior_recs = dict(prior.get("record_sigs") or {})
        for key in set(prior_recs) | set(rec_sig):
            if prior_recs.get(key) != rec_sig.get(key):
                a, b = key.split("|")[:2]
                changed |= {a, b} & set(node_sigs)
        if removed:
            c = self.writer.cols
            where = f"{c['a_model_id']}::text = ANY(%(gone)s) OR {c['b_model_id']}::text = ANY(%(gone)s)"
            r.m("compat_deletes", float(self._delete_keys(where, {"gone": removed}, active)))
            gone = set(removed)
            self._forget(lambda a, b, rel, rid: rid in active_set and (a in gone or b in gone))
        if not changed:
            r.metrics["compat_skipped"] = 1.0
            r.notes.append("compat: no node, record or rule changed: skipped")
            return new_state
        r.metrics["compat_mode_incremental"] = 1.0
        r.metrics["compat_changed_nodes"] = float(len(changed))
        kinds = {str(n.model_id): n.kind for n in nodes}
        if any(kinds.get(k) in b_kinds for k in changed):
            batch_a = a_ids
        else:
            batch_a = [n.model_id for n in nodes if n.kind in a_kinds and str(n.model_id) in changed]
        self._build(nodes, records, batch_a, changed=changed, rulesets=active)
        return new_state

    def _build(self, nodes: list[cg.ModelSpec], records: list[cg.FitRecord], a_ids: list[Any], *,
               changed: Optional[set[str]], rulesets: Sequence[str]) -> None:
        """Build, plan and write in batches of A-side models. Always planned
        with ``scope="full"``: a full build (``changed`` None) owns every row
        of its batch; an incremental one only the pairs touching ``changed``
        (compat_graph confines a partial build's deletes to
        ``GraphResult.covers``), where every row that no longer yields a
        stored edge is deleted, evaluated or not."""
        r = self.r
        size = int(self.cfg.get("batch_size", 10))
        only = None
        if changed is not None:
            ids = {str(n.model_id): n.model_id for n in nodes}
            only = [ids[k] for k in sorted(changed) if k in ids]
        for i in range(0, len(a_ids), size):
            batch = a_ids[i:i + size]
            r.say(f"4/5 compat: A models {i + 1}-{i + len(batch)} of {len(a_ids)}",
                  progress=0.82 + 0.08 * i / max(1, len(a_ids)))
            result = self.g.build(nodes, records, a_models=batch, only_models=only)
            for k, v in result.metrics().items():
                r.m(k, float(v))
            if result.unresolved:
                r.m("compat_unresolved", float(len(result.unresolved)))
            existing = self._existing(batch, result.rulesets or rulesets)
            built = set(result.rulesets)
            # the manual rows this batch derives (from origin-'manual' records)
            derived = {edge_key(*e.key): e for e in result.stored() if e.confidence == cg.CONF_MANUAL}

            def covered(a: Any, b: Any, rid: Any, _result: cg.GraphResult = result,
                        _built: set[str] = built) -> bool:
                return str(rid or "") in _built and _result.covers(a, b)

            releases, kept = [], []
            for row in existing:
                key = edge_key(*(row.get(k) for k in EDGE_KEY_FIELDS))
                if (str(row.get("confidence") or "") == cg.CONF_MANUAL and key not in derived
                        and covered(row.get("a_model_id"), row.get("b_model_id"), row.get("rule_id"))
                        and self._owns(row)):
                    releases.append(row)        # the engine's own row, no longer derived
                else:
                    kept.append(row)
            plan = self.g.plan(result, kept, scope="full")
            for w in plan.warnings[:3]:
                r.warnings.append(f"compat: {w}"[:200])
            applied, released = self._apply(plan, releases)
            # Inside the part this batch rebuilt, the engine owns exactly the
            # manual rows it derived (each was upserted or found unchanged);
            # every other entry there ended (released, edited, or gone).
            self._forget(lambda a, b, rel, rid, _d=derived: covered(a, b, rid)
                         and edge_key(a, b, rel, rid) not in _d)
            owned = self._owned()
            for key, edge in derived.items():
                owned[key] = edge_owned_sig(edge)
            r.m("compat_upserts", float(applied["upserted"]))
            r.m("compat_deletes", float(int(applied["deleted"]) + released))
            r.m("compat_unchanged", float(len(plan.unchanged)))
            r.m("compat_protected", float(len(plan.protected)))
