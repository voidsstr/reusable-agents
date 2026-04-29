#!/usr/bin/env python3
"""One-shot backfill: generate verification scripts for every shipped rec
that doesn't have one yet.

Walks every agent's runs/ tree, finds recs with shipped=true, and for each
one without an existing verifications/<rec_id>.json — invokes claude to
write a JS function that proves the change is in production. Saves to
agents/<source_agent>/runs/<run_ts>/verifications/<rec_id>.json.

Future implementer runs write these scripts at ship time; this script is
the one-time backfill for everything that shipped before the runbook
update landed.

Run:
  AZURE_STORAGE_CONNECTION_STRING=... STORAGE_BACKEND=azure \
      python3 scripts/backfill-verification-scripts.py [--limit N] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from framework.core.storage import get_storage  # noqa: E402


VERIFY_PROMPT_TEMPLATE = """You are writing a JavaScript verification function for the SpecPicks/AislePrompt
agent dashboard. The function will run client-side in the dashboard, with a
helper `proxyFetch(url)` available that returns `{{ok, status, body, headers}}`
(server-side fetch via the framework's API; bypasses browser CORS).

Your job: given a recommendation that was shipped to production, write a
function that PROVES the change is live. Be specific — don't just check
that the page returns 200; check for the SPECIFIC CONTENT this rec was
supposed to put there.

# The shipped recommendation

```json
{rec_json}
```

# Implementer's per-rec summary (what claude said it did)

{summary_md}

# Output contract — STRICT

Respond with ONLY a single JSON object, no prose, no markdown fences:

{{
  "explanation": "1-2 sentences explaining what you're checking and why it proves the change is live",
  "script_js": "async function verify({{ proxyFetch }}) {{\\n  // your code\\n  return {{ ok: true|false, evidence: ... }};\\n}}"
}}

# Rules for script_js

1. The function MUST be `async function verify({{ proxyFetch }})` exactly.
2. It returns `{{ ok: boolean, evidence: <any object> }}` — `evidence` should
   be the specific things you checked (e.g. matched <title>, found
   FAQPage schema, link-on-homepage=true).
3. Use `proxyFetch(url)` (not browser `fetch`) — it's the only way to
   read response bodies without CORS issues.
4. Be CONCRETE: if the rec said "rewrote SERP title to X", check that X
   appears in <title>. If it said "added FAQPage schema", check for
   '"@type":"FAQPage"' or 'FAQPage'.
5. Catch errors gracefully — don't throw. Always return ok+evidence.
6. Keep it ≤80 lines. Single fetch is usually enough; ≤3 fetches max.

Return ONLY the JSON object.
"""


def _build_prompt(rec: dict, summary_md: str) -> str:
    rec_pruned = {
        "id": rec.get("id"),
        "type": rec.get("type"),
        "priority": rec.get("priority"),
        "title": rec.get("title"),
        "rationale": (rec.get("rationale") or "")[:600],
        "url": rec.get("url"),
        "slug": rec.get("slug"),
        "article_proposal": rec.get("article_proposal"),
        "expected_impact": rec.get("expected_impact"),
        "shipped_via": rec.get("shipped_via"),
        "implemented_via": rec.get("implemented_via"),
        "implemented_commit": rec.get("implemented_commit"),
    }
    rec_pruned = {k: v for k, v in rec_pruned.items() if v is not None}
    return VERIFY_PROMPT_TEMPLATE.format(
        rec_json=json.dumps(rec_pruned, indent=2),
        summary_md=summary_md[:1500] if summary_md else "(no summary file)",
    )


def _invoke_claude(prompt: str) -> dict:
    proc = subprocess.run(
        ["claude", "--print", "--dangerously-skip-permissions",
         "--max-turns", "1", "--output-format", "text"],
        input=prompt, capture_output=True, text=True, timeout=180,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"claude rc={proc.returncode}: {proc.stderr[-200:]}")
    out = proc.stdout.strip()
    if out.startswith("```"):
        out = out.strip("`").strip()
        if out.startswith("json\n"):
            out = out[5:]
    return json.loads(out)


def _find_summary(s, source_agent: str, run_ts: str, rec_id: str) -> str:
    """Pull the implementer's per-rec summary.md if present. The implementer
    writes it under the DISPATCH run_ts (could be a `rundir-...` prefixed
    key) — try a few locations."""
    # Direct under source run
    for key in (
        f"agents/{source_agent}/runs/{run_ts}/changes/{rec_id}.summary.md",
    ):
        try:
            txt = s.read_text(key)
            if txt:
                return txt
        except Exception:
            pass
    # Search dispatch run dirs that match this source run
    try:
        for k in s.list_prefix(f"agents/{source_agent}/runs/"):
            if f"/changes/{rec_id}.summary.md" in k and run_ts in k:
                txt = s.read_text(k)
                if txt:
                    return txt
    except Exception:
        pass
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0,
                    help="cap how many backfills to do this run (default 0 = all)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="regenerate even if a verification.json already exists")
    args = ap.parse_args()

    s = get_storage()

    # Find every shipped rec across all agents
    shipped: list[tuple[str, str, str, dict]] = []  # (agent, run_ts, rec_id, rec)
    for k in s.list_prefix("agents/"):
        if not k.endswith("/recommendations.json"):
            continue
        parts = k.split("/")
        if len(parts) < 5 or parts[2] != "runs":
            continue
        agent_id = parts[1]
        run_ts = parts[3]
        # Only process source-agent run dirs (not implementer's dispatch dirs)
        if "rundir-" in run_ts:
            continue
        try:
            d = s.read_json(k) or {}
        except Exception:
            continue
        for r in d.get("recommendations", []):
            if not r.get("shipped"):
                continue
            shipped.append((agent_id, run_ts, r.get("id"), r))

    print(f"found {len(shipped)} shipped rec(s)")

    todo = []
    for agent_id, run_ts, rec_id, rec in shipped:
        existing_key = f"agents/{agent_id}/runs/{run_ts}/verifications/{rec_id}.json"
        if not args.force:
            try:
                if s.exists(existing_key):
                    continue
            except Exception:
                pass
        todo.append((agent_id, run_ts, rec_id, rec))

    print(f"{len(todo)} need backfill")
    if args.limit > 0:
        todo = todo[: args.limit]
        print(f"capped to {len(todo)} this run")

    if args.dry_run:
        for agent_id, run_ts, rec_id, _ in todo:
            print(f"  would generate: agents/{agent_id}/runs/{run_ts}/verifications/{rec_id}.json")
        return

    ok_count = 0
    fail_count = 0
    for agent_id, run_ts, rec_id, rec in todo:
        summary_md = _find_summary(s, agent_id, run_ts, rec_id)
        prompt = _build_prompt(rec, summary_md)
        print(f"\n→ {agent_id} {rec_id} ({rec.get('type','?')})")
        try:
            resp = _invoke_claude(prompt)
            if "script_js" not in resp:
                raise ValueError(f"no script_js in claude response: keys={list(resp.keys())}")
            doc = {
                "rec_id": rec_id,
                "rec_type": rec.get("type"),
                "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "generated_by": "backfill",
                "explanation": resp.get("explanation", ""),
                "script_js": resp["script_js"],
            }
            s.write_json(f"agents/{agent_id}/runs/{run_ts}/verifications/{rec_id}.json", doc)
            print(f"  ✓ wrote verification ({len(resp['script_js'])} chars of JS)")
            ok_count += 1
        except Exception as e:
            print(f"  ✗ failed: {e}")
            fail_count += 1

    print(f"\nbackfill complete: ok={ok_count} fail={fail_count}")


if __name__ == "__main__":
    main()
