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


class LLMStream:
    """Append-only stream of LLM I/O for one agent run."""

    def __init__(self, agent_id: str, run_ts: str,
                 storage: Optional[StorageBackend] = None):
        self.agent_id = agent_id
        self.run_ts = run_ts
        self.storage = storage or get_storage()
        self.key = llm_stream_key(agent_id, run_ts)

    def _append(self, record: dict) -> None:
        try:
            self.storage.append_jsonl(self.key, record)
        except Exception as e:
            logger.warning("llm-stream append failed (%s): %s", self.key, e)

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


def stream_for_current_run(storage: Optional[StorageBackend] = None) -> Optional[LLMStream]:
    """Build a stream from env vars set by the host-worker. Returns None
    when invoked outside an agent run (e.g. unit tests, ad-hoc scripts)
    so callers can no-op cleanly."""
    agent_id = os.environ.get("AGENT_ID")
    run_ts = os.environ.get("AGENT_RUN_ID") or os.environ.get("RUN_ID")
    if not agent_id or not run_ts:
        return None
    return LLMStream(agent_id, run_ts, storage=storage)


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
    s = LLMStream(agent_id, run_ts, storage=storage)
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
