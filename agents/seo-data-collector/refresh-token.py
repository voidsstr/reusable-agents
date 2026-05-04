#!/usr/bin/env python3
"""Google OAuth token manager for the seo-data-collector.

Stores {client_id, client_secret, refresh_token} at the path passed via
--oauth-file (or $SEO_AGENT_OAUTH_FILE, default
~/.reusable-agents/seo/.oauth.json), and prints a fresh 1-hour access
token on each invocation.

Usage:
  python3 refresh-token.py [--oauth-file PATH]              # print fresh token
  python3 refresh-token.py [--oauth-file PATH] --check      # validate without printing
  python3 refresh-token.py [--oauth-file PATH] --bootstrap  # run OAuth consent flow once
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request

DEFAULT_OAUTH_PATH = os.path.expanduser("~/.reusable-agents/seo/.oauth.json")
SCOPES = [
    "https://www.googleapis.com/auth/analytics.readonly",
    # 'webmasters' is the WRITE scope — superset of 'webmasters.readonly'.
    # Needed for Sitemaps.submit (used by gsc-coverage-auditor on deploys
    # to register sitemaps with GSC and accelerate URL discovery on
    # young/low-authority domains where 96%+ of URLs come back as
    # "URL is unknown to Google" otherwise).
    "https://www.googleapis.com/auth/webmasters",
    # adwords scope is requested too so a single OAuth bootstrap covers
    # Google Ads in addition to GSC + GA4. Existing oauth.json files
    # without this scope still work for GSC/GA4; pull_google_ads will
    # cleanly skip when the access token can't authorize the Ads API.
    "https://www.googleapis.com/auth/adwords",
]


def load_oauth(path: str) -> dict:
    if not os.path.exists(path):
        raise SystemExit(
            f"No OAuth file at {path}. "
            "Run `python3 refresh-token.py --bootstrap` first."
        )
    with open(path) as f:
        return json.load(f)


def save_oauth(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(path, 0o600)


def refresh_access_token(cfg: dict) -> str:
    body = urllib.parse.urlencode({
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "refresh_token": cfg["refresh_token"],
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())
    if "access_token" not in data:
        raise RuntimeError(f"Refresh failed: {data}")
    return data["access_token"]


def bootstrap(oauth_path: str) -> None:
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("Installing google-auth-oauthlib...", file=sys.stderr)
        import subprocess
        subprocess.check_call([
            sys.executable, "-m", "pip", "install",
            "google-auth-oauthlib", "-q",
        ])
        from google_auth_oauthlib.flow import InstalledAppFlow

    client_id = os.environ.get("SEO_AGENT_CLIENT_ID")
    client_secret = os.environ.get("SEO_AGENT_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise SystemExit(
            "Set SEO_AGENT_CLIENT_ID and SEO_AGENT_CLIENT_SECRET env vars first.\n\n"
            "Get them by creating an OAuth 2.0 Desktop credential at:\n"
            "  https://console.cloud.google.com/apis/credentials\n"
            "Then enable both Search Console API and Google Analytics Data API\n"
            "for the project."
        )

    flow = InstalledAppFlow.from_client_config({
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }, scopes=SCOPES)
    creds = flow.run_local_server(
        port=0, open_browser=True, access_type="offline", prompt="consent",
    )
    if not creds.refresh_token:
        raise SystemExit(
            "No refresh_token returned by Google. "
            "Try again and ensure you granted offline access."
        )
    save_oauth(oauth_path, {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": creds.refresh_token,
    })
    print(f"OAuth bootstrapped. Saved to {oauth_path}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--bootstrap", action="store_true")
    p.add_argument("--check", action="store_true")
    p.add_argument("--oauth-file",
                   default=os.environ.get("SEO_AGENT_OAUTH_FILE", DEFAULT_OAUTH_PATH),
                   help="Path to .oauth.json (default: ~/.reusable-agents/seo/.oauth.json)")
    args = p.parse_args()

    if args.bootstrap:
        bootstrap(args.oauth_file)
        return

    cfg = load_oauth(args.oauth_file)
    try:
        token = refresh_access_token(cfg)
    except Exception as e:
        print(f"Token refresh failed: {e}", file=sys.stderr)
        sys.exit(2)

    if args.check:
        print(f"OK ({len(token)} chars)")
        return

    print(token)


if __name__ == "__main__":
    main()
