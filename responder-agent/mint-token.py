#!/usr/bin/env python3
"""Mint a fresh access token from the saved refresh_token.

Two callers:
1. responder.py — imports as a function (mint_access_token).
2. msmtp via passwordeval — `python3 mint-token.py` prints the token to
   stdout. msmtp uses it as the SMTP auth password (XOAUTH2).

Usage:
  python3 mint-token.py                   # prints fresh access token
  python3 mint-token.py --check           # validates without printing
  python3 mint-token.py --imap-username   # prints just the username (for SMTP/IMAP user line)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_OAUTH_PATH = Path(os.path.expanduser("~/.reusable-agents/responder/.oauth.json"))


def load_oauth(path: Path) -> dict:
    if not path.is_file():
        raise SystemExit(
            f"No OAuth file at {path}.\n"
            f"Run `python3 oauth-bootstrap.py --provider microsoft|google …` first."
        )
    return json.loads(path.read_text())


def mint_microsoft(cfg: dict, scope_override: str = None) -> str:
    # Microsoft v2 token endpoint mints tokens for one resource per request.
    # IMAP uses outlook.office.com; Graph uses graph.microsoft.com.
    # Pass scope_override='offline_access https://graph.microsoft.com/Mail.Send'
    # to get a Graph-audience token from the same refresh token (cross-resource
    # refresh; works as long as the refresh_token includes offline_access).
    body = urllib.parse.urlencode({
        "client_id": cfg["client_id"],
        "refresh_token": cfg["refresh_token"],
        "grant_type": "refresh_token",
        "scope": scope_override or cfg.get("scopes", " ".join([
            "offline_access",
            "https://outlook.office.com/IMAP.AccessAsUser.All",
            "https://outlook.office.com/SMTP.Send",
        ])),
    }).encode()
    url = f"https://login.microsoftonline.com/{cfg.get('tenant', 'organizations')}/oauth2/v2.0/token"
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        tokens = json.loads(resp.read().decode())
    if "access_token" not in tokens:
        raise SystemExit(f"Refresh failed: {tokens}")
    # Microsoft sometimes rotates the refresh token — persist if so
    if "refresh_token" in tokens and tokens["refresh_token"] != cfg["refresh_token"]:
        cfg["refresh_token"] = tokens["refresh_token"]
        DEFAULT_OAUTH_PATH.write_text(json.dumps(cfg, indent=2))
    return tokens["access_token"]


def mint_google(cfg: dict, scope_override: str = None) -> str:  # noqa: ARG001
    # Google ignores scope on refresh and returns the original scopes.
    body = urllib.parse.urlencode({
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "refresh_token": cfg["refresh_token"],
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token", data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        tokens = json.loads(resp.read().decode())
    if "access_token" not in tokens:
        raise SystemExit(f"Refresh failed: {tokens}")
    return tokens["access_token"]


def mint_access_token(
    oauth_path: Path = DEFAULT_OAUTH_PATH,
    scope_override: str = None,
) -> tuple[str, str, str]:
    """Returns (access_token, username, provider).

    scope_override lets callers ask for a token with different scopes than
    were originally granted — useful for getting a Graph-audience token from
    a refresh_token originally minted for Outlook IMAP/SMTP scopes. Microsoft's
    refresh-token-grant supports cross-resource scope substitution as long as
    the original consent included offline_access.
    """
    cfg = load_oauth(oauth_path)
    if cfg["provider"] == "microsoft":
        token = mint_microsoft(cfg, scope_override=scope_override)
    elif cfg["provider"] == "google":
        token = mint_google(cfg, scope_override=scope_override)
    else:
        raise SystemExit(f"Unknown provider: {cfg.get('provider')!r}")
    return token, cfg.get("username_hint", ""), cfg["provider"]


def build_xoauth2_string(username: str, access_token: str) -> bytes:
    """Build the SASL XOAUTH2 init string. This is the exact bytes you pass
    to imaplib.IMAP4.authenticate('XOAUTH2', lambda _: <bytes>)."""
    return f"user={username}\x01auth=Bearer {access_token}\x01\x01".encode()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--check", action="store_true")
    p.add_argument("--username", action="store_true",
                   help="Print just the username from the saved config")
    p.add_argument("--xoauth2", action="store_true",
                   help="Print the XOAUTH2 SASL init string (base64-ready) instead of raw token")
    p.add_argument("--oauth-file", default=str(DEFAULT_OAUTH_PATH))
    args = p.parse_args()

    cfg = load_oauth(Path(args.oauth_file))
    if args.username:
        print(cfg.get("username_hint", ""))
        return

    token, username, provider = mint_access_token(Path(args.oauth_file))

    if args.check:
        print(f"OK provider={provider} user={username} token_chars={len(token)}")
        return

    if args.xoauth2:
        if not username:
            raise SystemExit("username_hint not set in oauth file — pass --username during bootstrap")
        import base64
        sasl = build_xoauth2_string(username, token)
        print(base64.b64encode(sasl).decode())
        return

    print(token)


if __name__ == "__main__":
    main()
