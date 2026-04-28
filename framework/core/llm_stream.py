"""LLM I/O streaming — centralized capture of every prompt/response for
the dashboard's Live LLM panel.

Every AIClient.chat() call appends three records to:
  agents/<agent_id>/runs/<run_ts>/llm-output.jsonl

Records are JSONL with shape:
  {"ts": "...", "kind": "request",  "model": "...", "system": "...",
   "user": "...", "messages": [...], "meta": {...}}
  {"ts": "...", "kind": "chunk",    "text": "...", "elapsed_s": 12.4}      (optional, streaming providers)
  {"ts": "...", "kind": "response", "text": "...", "duration_s": 47.2,
   "provider": "...", "kind_provider": "...", "meta": {...}}
  {"ts": "...", "kind": "error",    "msg": "...", "duration_s": 2.1}

Stored via the framework's get_storage(), so it lands in whatever backend
the operator configured (Azure blob in production). The dashboard's API
endpoint reads from the same key — same data regardless of which host
ran the agent. This is the durable replacement for the old
local-/tmp/reusable-agents-logs/ tail-file pattern that didn't work for
Python-driven agents (whose claude --print output was captured into a
Python string instead of piped through to the host-worker's redirect).

Subclasses of AIClient call:
  with self.stream_call(messages, model=...) as call:
      raw = ...invoke backend...
      call.response(raw)

…and the wrapper handles request/response logging + duration tracking +
error capture automatically. Existing callers don't change.

Live tail blob — fast-path for the dashboard's poll loop
--------------------------------------------------------

The JSONL file grows unbounded (one line per request/chunk/response). To
avoid forcing the dashboard to download + parse a megabyte-sized JSONL
on every 2-second poll, every append also bumps a debounced-write
companion blob:

  agents/<agent_id>/live-llm-output.txt

Layout: a single `__META__: {...}` JSON header line followed by the
last ~256KB of *rendered* (human-readable) stream content. Rendered
once on the agent host at flush time; the dashboard does ONE blob read
and shows the bytes verbatim, with the meta header letting it surface
"is_active=true/false", "started_at", "updated_at" alongside.

Writes are throttled by `LiveLLMTail` (default once per 3s) — chunk
records appended faster than that don't trigger extra blob writes.
"""
from __future__ import annotations

import json
import logging
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

from .storage import StorageBackend, get_storage


logger = logging.getLogger("framework.llm-stream")

# Cap a single record's text payload so a runaway agent can't fill blob
# storage with one massive prompt/response in a single record. Truncated
# entries get a "...truncated" marker so the dashboard shows it cleanly.
MAX_RECORD_CHARS = int(os.environ.get("LLM_STREAM_MAX_CHARS", "200000"))


def llm_stream_key(agent_id: str, run_ts: str) -> str:
    return f"agents/{agent_id}/runs/{run_ts}/llm-output.jsonl"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _truncate(s: Any) -> str:
    if not isinstance(s, str):
        try:
            s = json.dumps(s, default=str)
        except Exception:
            s = str(s)
    if len(s) > MAX_RECORD_CHARS:
        return s[:MAX_RECORD_CHARS] + f"\n…[truncated; original was {len(s)} chars]"
    return s


def live_llm_tail_key(agent_id: str) -> str:
    """Per-agent flat live-tail blob — what the dashboard polls."""
    return f"agents/{agent_id}/live-llm-output.txt"


# Cap the live-tail blob so the dashboard read is always cheap.
LIVE_TAIL_MAX_BYTES = int(os.environ.get("LLM_STREAM_LIVE_TAIL_BYTES", "262144"))  # 256 KB
LIVE_TAIL_FLUSH_S   = float(os.environ.get("LLM_STREAM_LIVE_TAIL_FLUSH_S", "3.0"))


def _render_records_for_tail(records: list[dict]) -> str:
    """Render a list of stream records as the human-readable log the
    dashboard's `<pre>` element shows. Same shape the API endpoint
    produces for the JSONL fallback path — mirrored here so the live
    tail blob is drop-in compatible.
    """
    out: list[str] = []
    for r in records:
        kind = r.get("kind")
        ts = r.get("ts", "")
        # HH:MM:SS prefix — short, fixed-width, dashboard-friendly.
        tshort = ts[11:19] if len(ts) >= 19 else ts
        if kind == "request":
            out.append(f"\n[{tshort}] === REQUEST · {r.get('model','')} "
                       f"({(r.get('meta') or {}).get('provider','')}) ===")
            if r.get("system"):
                out.append(f"\n[{tshort}] [SYSTEM]\n{r['system']}")
            if r.get("user"):
                out.append(f"\n[{tshort}] [USER]\n{r['user']}\n")
        elif kind == "chunk":
            text = r.get("text", "")
            if text:
                # Prefix each non-empty line with the chunk's timestamp so
                # log staleness is visible per line in the dashboard.
                lines = text.split("\n")
                stamped = [f"[{tshort}] {ln}" if ln else "" for ln in lines]
                out.append("\n".join(stamped))
        elif kind == "response":
            meta = r.get("meta") or {}
            out.append(f"\n[{tshort}] === RESPONSE · {meta.get('model','')} · "
                       f"{r.get('duration_s', 0):.1f}s ===")
            out.append(r.get("text", ""))
            out.append("\n---\n")
        elif kind == "error":
            out.append(f"\n[{tshort}] === ERROR ===\n{r.get('msg','')}\n---\n")
    return "".join(out)


def _read_records_for_run(storage: StorageBackend, agent_id: str,
                          run_ts: str) -> list[dict]:
    text = storage.read_text(llm_stream_key(agent_id, run_ts)) or ""
    out: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


class LiveLLMTail:
    """Debounced writer for `agents/<id>/live-llm-output.txt`.

    Lives one-per-LLMStream so we share the throttle clock across all
    request/chunk/response/error appends in a run. Writes happen in the
    foreground but at most once per `flush_interval_s`; if a caller
    appends faster than that, intermediate writes are coalesced.

    Final flush (with `is_active=False`) is invoked from `finalize()` —
    LLMStream.response() / LLMStream.error() / agent_base.post_run all
    call it. Atexit-handler also calls it as a safety net.
    """

    def __init__(self, agent_id: str, run_ts: str,
                 storage: Optional[StorageBackend] = None,
                 flush_interval_s: float = LIVE_TAIL_FLUSH_S,
                 max_bytes: int = LIVE_TAIL_MAX_BYTES):
        self.agent_id = agent_id
        self.run_ts = run_ts
        self.storage = storage or get_storage()
        self.key = live_llm_tail_key(agent_id)
        self.flush_interval_s = flush_interval_s
        self.max_bytes = max_bytes
        self.started_at = _now_iso()
        self._last_flush_at = 0.0
        # Whether finalize() already wrote `is_active=False` — guards
        # against duplicate "ended" writes from finalize + atexit.
        self._closed = False

    def _build_body(self, *, is_active: bool) -> str:
        # Render all records for the run; cap the text at max_bytes
        # (keep the END of the rendered output, that's the latest claude
        # output the operator wants to see).
        try:
            records = _read_records_for_run(self.storage, self.agent_id, self.run_ts)
        except Exception:
            records = []
        rendered = _render_records_for_tail(records)
        if len(rendered.encode("utf-8")) > self.max_bytes:
            # Truncate from the front — keep the tail (latest output).
            tail_bytes = rendered.encode("utf-8")[-self.max_bytes:]
            # Drop a possibly-partial first line.
            i = tail_bytes.find(b"\n")
            if i >= 0 and i < 2048:
                tail_bytes = tail_bytes[i + 1:]
            rendered = b"...[truncated; older output dropped]\n".decode() + \
                       tail_bytes.decode("utf-8", errors="replace")
        meta = {
            "agent_id":   self.agent_id,
            "run_ts":     self.run_ts,
            "started_at": self.started_at,
            "updated_at": _now_iso(),
            "is_active":  is_active,
            "n_records":  len(records),
            "tail_bytes": len(rendered.encode("utf-8")),
        }
        return "__META__: " + json.dumps(meta) + "\n" + rendered

    def _write(self, *, is_active: bool) -> None:
        try:
            body = self._build_body(is_active=is_active)
            # cache_control=2s — actively-updated blob; browsers can use
            # a tiny window without serving stale snapshots.
            self.storage.write_text(
                self.key, body,
                cache_control="public, max-age=2",
            )
            self._last_flush_at = time.time()
        except Exception as e:
            logger.warning("live-tail write failed (%s): %s", self.key, e)

    def maybe_flush(self) -> None:
        """Throttled flush — called after each append. Skips if last
        flush was within `flush_interval_s`."""
        if self._closed:
            return
        now = time.time()
        if (now - self._last_flush_at) >= self.flush_interval_s:
            self._write(is_active=True)

    def finalize(self) -> None:
        """Final flush with is_active=False. Idempotent."""
        if self._closed:
            return
        self._closed = True
        self._write(is_active=False)


class LLMStream:
    """Append-only stream of LLM I/O for one agent run."""

    def __init__(self, agent_id: str, run_ts: str,
                 storage: Optional[StorageBackend] = None):
        self.agent_id = agent_id
        self.run_ts = run_ts
        self.storage = storage or get_storage()
        self.key = llm_stream_key(agent_id, run_ts)
        # Companion live-tail writer; same storage backend so the
        # dashboard read & the agent write go to the same place.
        self.live_tail = LiveLLMTail(agent_id, run_ts, storage=self.storage)

    def _append(self, record: dict) -> None:
        try:
            self.storage.append_jsonl(self.key, record)
        except Exception as e:
            logger.warning("llm-stream append failed (%s): %s", self.key, e)
        # After every append, ask the live-tail writer to maybe-flush
        # (no-op if last write was within `flush_interval_s`).
        try:
            self.live_tail.maybe_flush()
        except Exception:
            pass

    def finalize(self) -> None:
        """Mark the run finished. Writes one final live-tail with
        is_active=False so the dashboard can show 'last run ended N
        min ago' instead of a stale ● tailing badge."""
        try:
            self.live_tail.finalize()
        except Exception:
            pass

    # ── Public API ─────────────────────────────────────────────────
    def request(self, *, model: str, messages: list[dict], **meta) -> None:
        sys_msg = next((m for m in messages if m.get("role") == "system"), None)
        usr_msg = next((m for m in messages if m.get("role") == "user"), None)
        rec = {
            "ts": _now_iso(),
            "kind": "request",
            "agent_id": self.agent_id,
            "run_ts": self.run_ts,
            "model": model,
            "system": _truncate(sys_msg.get("content")) if sys_msg else "",
            "user": _truncate(usr_msg.get("content")) if usr_msg else "",
            "n_messages": len(messages),
            "meta": meta or {},
        }
        self._append(rec)

    def chunk(self, text: str, *, elapsed_s: float = 0.0) -> None:
        if not text: return
        self._append({
            "ts": _now_iso(),
            "kind": "chunk",
            "agent_id": self.agent_id,
            "run_ts": self.run_ts,
            "text": _truncate(text),
            "elapsed_s": round(float(elapsed_s), 2),
        })

    def response(self, text: str, *, duration_s: float = 0.0, **meta) -> None:
        self._append({
            "ts": _now_iso(),
            "kind": "response",
            "agent_id": self.agent_id,
            "run_ts": self.run_ts,
            "text": _truncate(text),
            "duration_s": round(float(duration_s), 2),
            "meta": meta or {},
        })

    def error(self, msg: str, *, duration_s: float = 0.0) -> None:
        self._append({
            "ts": _now_iso(),
            "kind": "error",
            "agent_id": self.agent_id,
            "run_ts": self.run_ts,
            "msg": _truncate(msg),
            "duration_s": round(float(duration_s), 2),
        })


# Process-level cache so all callers within one run share the same
# LiveLLMTail instance (and therefore the same throttle clock). Without
# this, an agent that calls `stream_for_current_run()` from N places
# would build N independent throttled writers — they'd each "fire" on
# their own timer and potentially write the same blob N times in quick
# succession.
_STREAM_CACHE: dict[tuple[str, str], LLMStream] = {}
_STREAM_CACHE_LOCK = __import__("threading").Lock()
_ATEXIT_REGISTERED = False


def stream_for_current_run(storage: Optional[StorageBackend] = None) -> Optional[LLMStream]:
    """Build a stream from env vars set by the host-worker. Returns None
    when invoked outside an agent run (e.g. unit tests, ad-hoc scripts)
    so callers can no-op cleanly."""
    agent_id = os.environ.get("AGENT_ID")
    run_ts = os.environ.get("AGENT_RUN_ID") or os.environ.get("RUN_ID")
    if not agent_id or not run_ts:
        return None
    key = (agent_id, run_ts)
    with _STREAM_CACHE_LOCK:
        s = _STREAM_CACHE.get(key)
        if s is None:
            s = LLMStream(agent_id, run_ts, storage=storage)
            _STREAM_CACHE[key] = s
        global _ATEXIT_REGISTERED
        if not _ATEXIT_REGISTERED:
            import atexit
            atexit.register(_finalize_all_streams)
            _ATEXIT_REGISTERED = True
    return s


def _finalize_all_streams() -> None:
    """atexit safety net — flip every cached stream's live-tail to
    is_active=false on process exit. Idempotent."""
    with _STREAM_CACHE_LOCK:
        for s in _STREAM_CACHE.values():
            try: s.finalize()
            except Exception: pass


def finalize_current_run(*, storage: Optional[StorageBackend] = None) -> None:
    """Public API: agents call this from post_run() (or run.sh after
    `claude --print` finishes) to mark the live-tail blob as inactive
    immediately, instead of waiting for atexit."""
    agent_id = os.environ.get("AGENT_ID")
    run_ts = os.environ.get("AGENT_RUN_ID") or os.environ.get("RUN_ID")
    if not agent_id or not run_ts:
        return
    with _STREAM_CACHE_LOCK:
        s = _STREAM_CACHE.get((agent_id, run_ts))
    if s is not None:
        s.finalize()


@contextmanager
def stream_call(*, agent_id: Optional[str] = None,
                run_ts: Optional[str] = None,
                model: str = "",
                messages: Optional[list[dict]] = None,
                provider: str = "",
                kind_provider: str = "",
                storage: Optional[StorageBackend] = None) -> Iterator[Optional[LLMStream]]:
    """Wrap one chat() call. Logs request → yields the stream → captures
    duration on exit. Caller calls `stream.response(text)` (or `.error`)
    once they have the assistant text. If they don't (e.g. early raise),
    the wrapper still records the error.

    Use:
        with stream_call(model='...', messages=msgs) as s:
            text = backend.invoke(...)
            if s: s.response(text)
            return text
    """
    if agent_id is None or run_ts is None:
        env_id = os.environ.get("AGENT_ID")
        env_ts = os.environ.get("AGENT_RUN_ID") or os.environ.get("RUN_ID")
        agent_id = agent_id or env_id
        run_ts = run_ts or env_ts
    if not agent_id or not run_ts:
        # No active run — yield None so callers can be no-ops.
        yield None
        return
    # Reuse the per-run cached LLMStream when possible so the live-tail
    # throttle clock is shared with other concurrent callers in the
    # same run.
    with _STREAM_CACHE_LOCK:
        s = _STREAM_CACHE.get((agent_id, run_ts))
        if s is None:
            s = LLMStream(agent_id, run_ts, storage=storage)
            _STREAM_CACHE[(agent_id, run_ts)] = s
    if messages:
        s.request(model=model, messages=messages,
                  provider=provider, kind_provider=kind_provider)
    t0 = time.time()
    try:
        yield s
    except Exception as e:
        s.error(str(e), duration_s=time.time() - t0)
        raise


def read_stream(agent_id: str, run_ts: str,
                storage: Optional[StorageBackend] = None,
                since_offset: int = 0) -> tuple[list[dict], int]:
    """Read records for one agent run. Returns (records, byte_offset).
    `since_offset` lets the dashboard fetch only the new tail."""
    s = storage or get_storage()
    key = llm_stream_key(agent_id, run_ts)
    text = s.read_text(key) or ""
    if since_offset and since_offset < len(text):
        text = text[since_offset:]
    elif since_offset >= len(text):
        return [], len(text)
    out: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line: continue
        try: out.append(json.loads(line))
        except json.JSONDecodeError: continue
    return out, len(text) + since_offset if since_offset else len(text)


def latest_run_ts(agent_id: str,
                  storage: Optional[StorageBackend] = None) -> Optional[str]:
    """Pick the most recent run_ts for an agent — uses status.json's
    current_run_ts if available, otherwise scans run dirs."""
    s = storage or get_storage()
    status = s.read_json(f"agents/{agent_id}/status.json") or {}
    if status.get("current_run_ts"):
        return status["current_run_ts"]
    keys = s.list_prefix(f"agents/{agent_id}/runs/")
    run_ts_set = set()
    for k in keys:
        # extract <run_ts> from agents/<id>/runs/<run_ts>/something
        try: run_ts_set.add(k.split("/runs/")[1].split("/")[0])
        except Exception: continue
    return max(run_ts_set) if run_ts_set else None
