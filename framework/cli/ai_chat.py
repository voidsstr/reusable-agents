"""ai_chat — CLI entry point for invoking the agent's configured AI provider.

Lets bash agents (or any non-Python caller) make a single chat call
through the framework's provider system without needing to know which
backend is wired up.

Usage:

    # Pipe stdin → response on stdout
    echo "summarize the GSC data: ..." | \\
      python3 -m framework.cli.ai_chat --agent seo-implementer

    # Override provider for a one-off call
    python3 -m framework.cli.ai_chat --agent seo-implementer \\
       --provider anthropic --model claude-opus-4-7 --prompt "..."

    # Multi-message chat with a system prompt
    python3 -m framework.cli.ai_chat --agent seo-implementer \\
       --system "You summarize tersely." \\
       --prompt "Summarize this: ..."

Reads $AI_PROVIDER from env if set (used by the framework's host-worker
when a script is invoked via /api/agents/<id>/scripts/<name>).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Ensure framework is importable
_REPO = Path(__file__).resolve().parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from framework.core import ai_providers  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(prog="ai_chat",
                                 description="One-shot chat call via the framework's provider config")
    p.add_argument("--agent", required=True, help="Agent id (used to resolve the provider)")
    p.add_argument("--provider", default=os.getenv("AI_PROVIDER", ""),
                   help="Override provider name")
    p.add_argument("--model", default=os.getenv("AI_MODEL", ""),
                   help="Override model")
    p.add_argument("--system", default="", help="System prompt (optional)")
    p.add_argument("--prompt", default="",
                   help="User prompt. If omitted, read from stdin.")
    p.add_argument("--temperature", type=float, default=0.0)
    p.add_argument("--max-tokens", type=int, default=2048)
    args = p.parse_args()

    user_prompt = args.prompt
    if not user_prompt and not sys.stdin.isatty():
        user_prompt = sys.stdin.read()
    if not user_prompt.strip():
        print("ERROR: no prompt provided (use --prompt or pipe via stdin)", file=sys.stderr)
        sys.exit(2)

    try:
        client = ai_providers.ai_client_for(
            args.agent,
            override_provider=args.provider or None,
            override_model=args.model or None,
        )
    except Exception as e:
        print(f"ERROR: could not resolve provider: {e}", file=sys.stderr)
        sys.exit(3)

    messages = []
    if args.system:
        messages.append({"role": "system", "content": args.system})
    messages.append({"role": "user", "content": user_prompt})

    try:
        resp = client.chat(messages,
                           temperature=args.temperature,
                           max_tokens=args.max_tokens)
    except Exception as e:
        print(f"ERROR: chat call failed: {e}", file=sys.stderr)
        sys.exit(4)

    print(resp)


if __name__ == "__main__":
    main()
