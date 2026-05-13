"""Bash-callable entrypoint for the code-editor backend chain.

Used by `agents/implementer/run.sh` (and any future agent that needs
LLM-driven file edits) so we don't hardcode the aider invocation —
the chain is configurable per deployment / per agent / per site.

Usage (typical implementer dispatch):

    python3 -m framework.cli.code_edit \\
        --repo /home/voidsstr/development/specpicks \\
        --prompt-file /tmp/aider-prompt.txt \\
        --files-file /tmp/aider-files.txt \\
        --pre-dirty-file /tmp/pre-dirty.txt \\
        --agent-id seo-implementer \\
        --site specpicks \\
        --site-config /home/voidsstr/development/specpicks/agents/.../site.yaml \\
        --json

Exit codes:
  0  → at least one backend in the chain succeeded
  2  → invalid args
  3  → all backends in the chain failed or skipped

stdout (with --json): a single JSON object summarising the run:
  {
    "winner": "aider-copilot-proxy",
    "rc": 0,
    "duration_sec": 12.4,
    "files_changed": ["frontend/src/.../X.tsx"],
    "attempts": [
      {"backend_id": "...", "rc": 0|N, "skipped": false, "duration_sec": ...},
      ...
    ]
  }
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

from framework.core.code_editor import (
    EditRequest, build_backend, load_config, resolve_chain, run_with_fallback,
)


def _maybe_load_yaml(path: Optional[str]) -> Optional[dict]:
    if not path:
        return None
    p = Path(path)
    if not p.is_file():
        return None
    try:
        import yaml  # type: ignore
        return yaml.safe_load(p.read_text())
    except Exception as e:
        print(f"code_edit: warning — could not read site.yaml {p}: {e}",
              file=sys.stderr)
        return None


def _maybe_load_manifest(agent_id: Optional[str]) -> Optional[dict]:
    if not agent_id:
        return None
    try:
        from framework.core.storage import get_storage
        s = get_storage()
        m = s.read_json(f"agents/{agent_id}/manifest.json")
        return m if isinstance(m, dict) else None
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(prog="framework.cli.code_edit")
    ap.add_argument("--repo", required=True,
                    help="Repo root where the editor runs (CWD)")
    ap.add_argument("--prompt-file", required=True,
                    help="Path to focused per-rec prompt")
    ap.add_argument("--files-file", default="",
                    help="Path to file containing one preload file per line")
    ap.add_argument("--pre-dirty-file", default="",
                    help="Path to file listing pre-edit dirty files (excluded "
                         "from commit by the wrapper, NOT by this CLI)")
    ap.add_argument("--agent-id", default="",
                    help="Agent id for manifest-level chain override")
    ap.add_argument("--site", default="",
                    help="Site id (informational)")
    ap.add_argument("--site-config", default="",
                    help="Path to site.yaml for site-level chain override")
    ap.add_argument("--chain", default="",
                    help="Explicit comma-separated chain "
                         "(overrides config + manifest + site)")
    ap.add_argument("--timeout", type=int, default=600,
                    help="Per-backend timeout in seconds")
    ap.add_argument("--dispatch-kind", default="",
                    help="Hint: catalog-audit | article-author | seo | h2h "
                         "| progressive-improvement. Used by ollama-backed "
                         "backends to route to the right local model.")
    ap.add_argument("--json", action="store_true",
                    help="Emit summary JSON on stdout")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    repo = Path(args.repo).resolve()
    if not repo.is_dir():
        print(f"code_edit: --repo not a directory: {repo}", file=sys.stderr)
        return 2
    prompt = Path(args.prompt_file)
    if not prompt.is_file():
        print(f"code_edit: --prompt-file not found: {prompt}", file=sys.stderr)
        return 2

    files: list[str] = []
    if args.files_file:
        ff = Path(args.files_file)
        if ff.is_file():
            files = [ln.strip() for ln in ff.read_text().splitlines()
                     if ln.strip()]

    pre_dirty = Path(args.pre_dirty_file) if args.pre_dirty_file else None

    cfg = load_config()

    if args.chain:
        chain = [x.strip() for x in args.chain.split(",") if x.strip()]
    else:
        manifest = _maybe_load_manifest(args.agent_id)
        site_yaml = _maybe_load_yaml(args.site_config)
        chain = resolve_chain(
            manifest=manifest, site_yaml=site_yaml, config=cfg)

    if not chain:
        print("code_edit: empty chain", file=sys.stderr)
        return 2

    logging.info(f"code_edit: chain = {chain}")

    req = EditRequest(
        repo_path=repo,
        prompt_path=prompt,
        files=files,
        pre_dirty_path=pre_dirty,
        timeout_sec=args.timeout,
        dispatch_kind=args.dispatch_kind or os.environ.get("DISPATCH_KIND", ""),
    )
    winner, attempts = run_with_fallback(req, chain, config=cfg)

    summary = {
        "winner": winner.backend_id if winner.rc == 0 else None,
        "rc": winner.rc,
        "duration_sec": round(winner.duration_sec, 2),
        "files_changed": winner.files_changed,
        "log_excerpt": winner.log_excerpt,
        "attempts": [
            {
                "backend_id": a.backend_id,
                "rc": a.rc,
                "skipped": a.skipped,
                "skip_reason": a.skip_reason,
                "duration_sec": round(a.duration_sec, 2),
                "files_changed": a.files_changed,
            }
            for a in attempts
        ],
    }
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"winner={summary['winner']}  rc={summary['rc']}  "
              f"duration={summary['duration_sec']}s  "
              f"files_changed={len(summary['files_changed'])}")
        for a in summary["attempts"]:
            tag = "SKIP" if a["skipped"] else f"rc={a['rc']}"
            print(f"  {a['backend_id']}: {tag} "
                  f"({a['duration_sec']}s, {len(a['files_changed'])} files)"
                  + (f" — {a['skip_reason']}" if a["skipped"] else ""))

    return 0 if winner.rc == 0 else 3


if __name__ == "__main__":
    sys.exit(main())
