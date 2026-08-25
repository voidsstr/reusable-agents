#!/usr/bin/env python3
"""
Google OAuth token manager for the SEO pipeline (Search Console + Analytics).

RESTORED 2026-08-13 after the fleet-host loss. The original lived only on the
retired box: a bare `lib/` pattern in .gitignore (line 17) matches at ANY
depth, so agents/seo-opportunity-agent/lib/{collector,analyzer}/ was never
committed and died with the drive. Ported from the tracked ancestor
nsc-assistant/agents/_legacy-seo-opportunity-agent/refresh-token.py, plus the
--oauth-file argument every current caller passes:

  install/reauth-google-oauth.sh      python3 refresh-token.py --oauth-file X --bootstrap
  agents/oauth-heartbeat-agent        keeps the 7-day Testing-mode clock alive
  agents/site-goals-tracker           reads GSC metrics
  lib/collector/pull-data.py          pulls the GSC data the analyzer scores

The client_id/client_secret are NOT secret-by-obscurity here: they identify
the OAuth app, and the refresh_token in the 0600 oauth file is what grants
access. Resolution order for the pair: --oauth-file contents, then
SEO_AGENT_CLIENT_ID / SEO_AGENT_CLIENT_SECRET (which install/recover-
credentials.sh harvests into ~/.reusable-agents/secrets.env).

Usage:
  python3 refresh-token.py [--oauth-file P]             # print a fresh access token
  python3 refresh-token.py [--oauth-file P] --check     # validate without printing
  python3 refresh-token.py [--oauth-file P] --bootstrap # full consent flow (browser)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request

DEFAULT_OAUTH_PATH = os.path.expanduser("~/.reusable-agents/seo/.oauth.json")

# Google revokes refresh tokens after 7 days of non-use while the consent
# screen is in "Testing". Publishing it to "In production" is the durable fix;
# oauth-heartbeat-agent exists to reset the clock daily until then.
# GSC scope: `webmasters` is read-WRITE (includes Sitemaps.submit);
# `webmasters.readonly` can read coverage + search analytics but CANNOT submit
# sitemaps, which makes IndexNow's sitemap pings 403. reauth-google-oauth.sh has
# always documented read-write in its header while this list requested readonly
# — the header was right and the code was wrong. Default to read-write; set
# GSC_READONLY=1 to request the narrower scope instead.
_GSC_SCOPE = (
    "https://www.googleapis.com/auth/webmasters.readonly"
    if os.environ.get("GSC_READONLY") == "1"
    else "https://www.googleapis.com/auth/webmasters"
)
SCOPES = [
    "https://www.googleapis.com/auth/analytics.readonly",
    _GSC_SCOPE,
]


def load_oauth(path: str) -> dict:
    if not os.path.exists(path):
        raise SystemExit(
            f"No OAuth file at {path}. "
            f"Run: python3 {os.path.basename(__file__)} --oauth-file {path} --bootstrap"
        )
    with open(path) as f:
        return json.load(f)


def save_oauth(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(path, 0o600)


def _client_pair(cfg: dict | None = None) -> tuple[str, str]:
    cfg = cfg or {}
    cid = cfg.get("client_id") or os.environ.get("SEO_AGENT_CLIENT_ID", "")
    sec = cfg.get("client_secret") or os.environ.get("SEO_AGENT_CLIENT_SECRET", "")
    if not cid or not sec:
        raise SystemExit(
            "No OAuth client pair. Supply SEO_AGENT_CLIENT_ID + SEO_AGENT_CLIENT_SECRET "
            "(they are in ~/.reusable-agents/secrets.env — `set -a; . secrets.env; set +a`) "
            "or put them in the --oauth-file."
        )
    return cid, sec


def refresh_access_token(cfg: dict) -> str:
    cid, sec = _client_pair(cfg)
    body = urllib.parse.urlencode({
        "client_id": cid,
        "client_secret": sec,
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


def bootstrap(path: str) -> None:
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("Installing google-auth-oauthlib…", file=sys.stderr)
        import subprocess
        subprocess.check_call([
            sys.executable, "-m", "pip", "install", "google-auth-oauthlib", "-q",
        ])
        from google_auth_oauthlib.flow import InstalledAppFlow

    existing = {}
    if os.path.exists(path):
        try:
            existing = json.load(open(path))
        except Exception:
            existing = {}
    client_id, client_secret = _client_pair(existing)

    flow = InstalledAppFlow.from_client_config({
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }, scopes=SCOPES)
    # On a headless box — WSL with interop off, a server over SSH, a container —
    # there is no browser to launch: the opener fails with "Operation not
    # supported" and buries the consent URL in an error line. Detect that and
    # print the URL prominently instead. The local callback server still runs,
    # and WSL2/SSH forward localhost, so pasting the URL into a browser on the
    # host machine completes the flow normally.
    # NB: WSLg sets DISPLAY even though no Linux browser is installed, so a
    # bare DISPLAY check reports "has a browser" and the launch still fails.
    # Treat WSL as headless, and allow an explicit override anywhere else.
    def _is_wsl() -> bool:
        if os.environ.get("WSL_DISTRO_NAME"):
            return True
        try:
            return "microsoft" in open("/proc/version").read().lower()
        except Exception:
            return False

    headless = (
        os.environ.get("SEO_OAUTH_NO_BROWSER") == "1"
        or _is_wsl()
        or not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
                or os.environ.get("BROWSER"))
    )
    if headless:
        print("\n" + "=" * 72, file=sys.stderr)
        print("No browser on this machine — open the URL below on your desktop.", file=sys.stderr)
        print("This terminal keeps listening; the page redirects back when you approve.", file=sys.stderr)
        print("=" * 72 + "\n", file=sys.stderr)

    # access_type=offline + prompt=consent is what makes Google return a
    # refresh_token; without both you get an access token that dies in an hour.
    creds = flow.run_local_server(
        port=0,
        open_browser=not headless,
        authorization_prompt_message="Open this URL to authorize:\n\n{url}\n",
        access_type="offline",
        prompt="consent",
    )
    if not creds.refresh_token:
        raise SystemExit(
            "No refresh_token returned by Google. Re-run and ensure offline access "
            "was granted (revoke the app at myaccount.google.com/permissions and retry)."
        )
    save_oauth(path, {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": creds.refresh_token,
    })
    print(f"OAuth bootstrapped. Saved to {path}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--oauth-file", default=os.environ.get("SEO_OAUTH_FILE", DEFAULT_OAUTH_PATH))
    p.add_argument("--bootstrap", action="store_true")
    p.add_argument("--check", action="store_true")
    args = p.parse_args()

    path = os.path.expanduser(args.oauth_file)

    if args.bootstrap:
        bootstrap(path)
        return

    cfg = load_oauth(path)
    try:
        token = refresh_access_token(cfg)
    except Exception as e:
        # HTTP 400 invalid_grant here = the refresh token was revoked (7-day
        # Testing-mode expiry, or the app was re-consented elsewhere).
        print(f"Token refresh failed: {e}", file=sys.stderr)
        sys.exit(2)

    if args.check:
        print(f"OK ({len(token)} chars)")
    else:
        print(token)


if __name__ == "__main__":
    main()
