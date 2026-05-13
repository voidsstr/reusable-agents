#!/usr/bin/env python3
"""OAuth bootstrap for responder-agent IMAP/SMTP — Microsoft 365 + Google.

One-time setup. Opens a browser, you log in as the automation mailbox,
click Allow on the consent screen. We capture the refresh_token and save
it to ~/.reusable-agents/responder/.oauth.json (mode 0600).

After bootstrap, mint-token.py reads the refresh_token and produces a
fresh access_token on demand for both IMAP (responder.py) and SMTP (msmtp
via passwordeval).

Usage:
  python3 oauth-bootstrap.py --provider microsoft \\
      --client-id <azure-app-client-id> \\
      --tenant <tenant-id-or-'organizations'>

  python3 oauth-bootstrap.py --provider google \\
      --client-id <google-oauth-client-id> \\
      --client-secret <google-oauth-client-secret>

Microsoft setup (one-time):
  1. portal.azure.com → Azure Active Directory → App registrations → New
  2. Name: 'reusable-agents responder' (or whatever)
  3. Supported account types: 'Accounts in this organizational directory only'
  4. Redirect URI: Public client/native → http://localhost
  5. Authentication → Allow public client flows: yes
  6. API permissions → Add a permission. Add ALL of these (delegated):
       Microsoft Graph:
         - Mail.Send                  (reporter sends via Graph)
         - Mail.Send.Shared           (reporter sends from a shared mailbox)
         - offline_access
         - User.Read (auto-added)
       Microsoft Graph (legacy outlook.office.com resource — for IMAP/SMTP):
         - IMAP.AccessAsUser.All      (responder reads inbox)
         - SMTP.Send                  (optional fallback if Graph is blocked)
     If you don't see the outlook.office.com options, switch to "APIs my
     organization uses" tab and search for "Office 365 Exchange Online" —
     they're listed there.
  7. Grant admin consent for [tenant] (button on the API permissions page).
  8. Copy the Application (client) ID — that's what you pass to --client-id.
  9. The Directory (tenant) ID is also on the Overview page — pass to --tenant
     (or use 'organizations' for multi-tenant).

Google setup (one-time):
  1. console.cloud.google.com → APIs & Services → Credentials → Create
     Credentials → OAuth client ID → Desktop app
  2. Enable the Gmail API for the project
  3. Use https://mail.google.com/ scope (full Gmail IMAP/SMTP access)
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread


DEFAULT_OAUTH_PATH = Path(os.path.expanduser("~/.reusable-agents/responder/.oauth.json"))


# ---------------------------------------------------------------------------
# Local callback server (catches the auth-code redirect)
# ---------------------------------------------------------------------------

class _CallbackHandler(BaseHTTPRequestHandler):
    captured = {}

    def do_GET(self):  # noqa: N802 (BaseHTTPRequestHandler API)
        qs = urllib.parse.urlparse(self.path).query
        params = dict(urllib.parse.parse_qsl(qs))
        _CallbackHandler.captured.update(params)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        if "code" in params:
            self.wfile.write(
                b"<h1>OAuth callback received.</h1>"
                b"<p>You can close this window and return to the terminal.</p>"
            )
        else:
            self.wfile.write(
                b"<h1>OAuth error.</h1>"
                + f"<pre>{json.dumps(params, indent=2)}</pre>".encode()
            )

    def log_message(self, *_a):  # silence the access log
        pass


def _start_callback_server(port: int = 0) -> tuple[HTTPServer, int]:
    server = HTTPServer(("127.0.0.1", port), _CallbackHandler)
    actual_port = server.server_address[1]
    Thread(target=server.serve_forever, daemon=True).start()
    return server, actual_port


# ---------------------------------------------------------------------------
# Microsoft 365 OAuth
# ---------------------------------------------------------------------------

MS_AUTH = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize"
MS_TOKEN = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
# Microsoft AAD requires all scopes in a single token request to belong to
# ONE resource (graph.microsoft.com OR outlook.office.com, never both).
# We expose two scope sets:
#   • "graph"   — for sending email via Microsoft Graph /sendMail. This is
#                 what the reporter / comp-research / app-store-opportunity
#                 agents use today.
#   • "outlook" — for the responder's IMAP polling + legacy SMTP fallback.
# If you need both, run the bootstrap twice (different output paths) and
# point the responder + reporter at their respective oauth files.
MS_SCOPE_SETS = {
    "graph": " ".join([
        "offline_access",
        "https://graph.microsoft.com/Mail.Send",
        "https://graph.microsoft.com/Mail.Send.Shared",
    ]),
    "outlook": " ".join([
        "offline_access",
        "https://outlook.office.com/IMAP.AccessAsUser.All",
        "https://outlook.office.com/SMTP.Send",
    ]),
}
# Backwards compat — anything still importing MS_SCOPES gets the Graph set.
MS_SCOPES = MS_SCOPE_SETS["graph"]


def bootstrap_microsoft(client_id: str, tenant: str, scope_set: str = "graph") -> dict:
    scopes = MS_SCOPE_SETS[scope_set]
    server, port = _start_callback_server()
    redirect_uri = f"http://localhost:{port}"
    state = secrets.token_urlsafe(16)

    auth_url = MS_AUTH.format(tenant=tenant) + "?" + urllib.parse.urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "response_mode": "query",
        "scope": scopes,
        "state": state,
        "prompt": "consent",
    })
    print(f"\nOpening browser to: {auth_url}\n", file=sys.stderr)
    print(f"If a browser doesn't open, copy the URL above and paste it.\n", file=sys.stderr)
    print(f"Waiting for the redirect to http://localhost:{port}/ …\n", file=sys.stderr)
    webbrowser.open(auth_url)

    # Wait for the callback
    while "code" not in _CallbackHandler.captured and "error" not in _CallbackHandler.captured:
        pass
    server.shutdown()

    if "error" in _CallbackHandler.captured:
        raise SystemExit(f"OAuth error: {_CallbackHandler.captured}")
    if _CallbackHandler.captured.get("state") != state:
        raise SystemExit("OAuth state mismatch — possible CSRF, aborting.")

    code = _CallbackHandler.captured["code"]
    # Exchange code for tokens
    body = urllib.parse.urlencode({
        "client_id": client_id,
        "code": code,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
        "scope": scopes,
    }).encode()
    req = urllib.request.Request(
        MS_TOKEN.format(tenant=tenant),
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        tokens = json.loads(resp.read().decode())
    if "refresh_token" not in tokens:
        raise SystemExit(f"No refresh_token in response. Got: {list(tokens.keys())}")
    return {
        "provider": "microsoft",
        "client_id": client_id,
        "tenant": tenant,
        "refresh_token": tokens["refresh_token"],
        "username_hint": "",  # filled by mint-token if missing
        "scopes": scopes,
    }


# ---------------------------------------------------------------------------
# Google OAuth
# ---------------------------------------------------------------------------

GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_SCOPES = "https://mail.google.com/"


def bootstrap_google(client_id: str, client_secret: str) -> dict:
    server, port = _start_callback_server()
    redirect_uri = f"http://localhost:{port}"
    state = secrets.token_urlsafe(16)

    auth_url = GOOGLE_AUTH + "?" + urllib.parse.urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": GOOGLE_SCOPES,
        "state": state,
        "access_type": "offline",
        "prompt": "consent",
    })
    print(f"\nOpening browser to: {auth_url}\n", file=sys.stderr)
    webbrowser.open(auth_url)

    while "code" not in _CallbackHandler.captured and "error" not in _CallbackHandler.captured:
        pass
    server.shutdown()

    if "error" in _CallbackHandler.captured:
        raise SystemExit(f"OAuth error: {_CallbackHandler.captured}")

    code = _CallbackHandler.captured["code"]
    body = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }).encode()
    req = urllib.request.Request(
        GOOGLE_TOKEN, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        tokens = json.loads(resp.read().decode())
    if "refresh_token" not in tokens:
        raise SystemExit(f"No refresh_token returned. Try with prompt=consent and offline access.")
    return {
        "provider": "google",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": tokens["refresh_token"],
        "username_hint": "",
        "scopes": GOOGLE_SCOPES,
    }


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------

def save_oauth(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))
    os.chmod(path, 0o600)


# ---------------------------------------------------------------------------

def bootstrap_microsoft_device_code(client_id: str, tenant: str,
                                    scope_set: str = "graph") -> dict:
    """Microsoft device-code flow — no localhost callback needed.

    Prints a short code + URL. User visits the URL on any device
    (phone, laptop, etc), enters the code, signs in. We poll the token
    endpoint until the user finishes. Way more robust over SSH or
    behind firewalls than the redirect-based flow.

    Microsoft AAD enforces single-resource scopes per request — pick
    one of the predefined scope sets. Default "graph" enables
    `Mail.Send` for the email-sending agents; "outlook" enables IMAP
    + legacy SMTP for the responder's inbox poll.
    """
    scopes = MS_SCOPE_SETS[scope_set]
    DEVICE_URL = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/devicecode"
    body = urllib.parse.urlencode({
        "client_id": client_id,
        "scope": scopes,
    }).encode()
    req = urllib.request.Request(
        DEVICE_URL, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        device = json.loads(resp.read().decode())

    print("", file=sys.stderr)
    print("─" * 60, file=sys.stderr)
    print(" Visit this URL on any device with a browser:", file=sys.stderr)
    print("", file=sys.stderr)
    print(f"   {device['verification_uri']}", file=sys.stderr)
    print("", file=sys.stderr)
    print(f" Enter this code:   {device['user_code']}", file=sys.stderr)
    print("", file=sys.stderr)
    print(" Then sign in as the mailbox you want the agents to send", file=sys.stderr)
    print(" from (e.g. automation@northernsoftwareconsulting.com), and", file=sys.stderr)
    print(" click Allow on the consent screen.", file=sys.stderr)
    print("─" * 60, file=sys.stderr)
    print("", file=sys.stderr)

    interval = max(5, int(device.get("interval", 5)))
    deadline = time.time() + int(device.get("expires_in", 900))
    token_url = MS_TOKEN.format(tenant=tenant)
    while time.time() < deadline:
        time.sleep(interval)
        body = urllib.parse.urlencode({
            "client_id": client_id,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": device["device_code"],
        }).encode()
        try:
            req = urllib.request.Request(
                token_url, data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                tokens = json.loads(resp.read().decode())
            break
        except urllib.error.HTTPError as e:
            err = json.loads(e.read().decode("utf-8", "replace"))
            code = err.get("error", "")
            if code == "authorization_pending":
                continue
            if code == "slow_down":
                interval += 5
                continue
            raise SystemExit(f"OAuth device-flow error: {err}")
    else:
        raise SystemExit("Device code expired before sign-in completed.")

    if "refresh_token" not in tokens:
        raise SystemExit(f"No refresh_token in response. Got: {list(tokens.keys())}")
    return {
        "provider": "microsoft",
        "client_id": client_id,
        "tenant": tenant,
        "refresh_token": tokens["refresh_token"],
        "username_hint": "",
        "scopes": scopes,
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--provider", choices=["microsoft", "google"], required=True)
    p.add_argument("--client-id", required=True)
    p.add_argument("--client-secret", help="Required for Google; ignored for Microsoft public clients")
    p.add_argument("--tenant", default="organizations",
                   help="Microsoft tenant id, 'common', 'organizations', or 'consumers' (default: organizations)")
    p.add_argument("--username", help="The mailbox username (e.g. automation@example.com). Saved as username_hint.")
    p.add_argument("--device-code", action="store_true",
                   help="Use device-code flow (Microsoft only) — no localhost callback. Best over SSH.")
    p.add_argument("--scope-set", choices=["graph", "outlook"], default="graph",
                   help="Microsoft scope set. 'graph' (default) = Mail.Send via Microsoft Graph "
                        "(used by reporter agents). 'outlook' = IMAP + legacy SMTP (used by "
                        "responder agent for inbox polling). AAD requires single-resource per "
                        "token request; if you need both, run twice with --out paths.")
    p.add_argument("--out", default=str(DEFAULT_OAUTH_PATH))
    args = p.parse_args()

    if args.provider == "microsoft":
        if args.device_code:
            data = bootstrap_microsoft_device_code(args.client_id, args.tenant, args.scope_set)
        else:
            data = bootstrap_microsoft(args.client_id, args.tenant, args.scope_set)
    else:
        if not args.client_secret:
            raise SystemExit("--client-secret required for Google")
        data = bootstrap_google(args.client_id, args.client_secret)

    if args.username:
        data["username_hint"] = args.username

    save_oauth(Path(args.out), data)
    print(f"\n✓ OAuth bootstrapped — saved to {args.out}")
    print(f"  provider: {data['provider']}")
    if "tenant" in data:
        print(f"  tenant:   {data['tenant']}")
    print(f"  username: {data.get('username_hint') or '(set --username next time)'}")
    print(f"\nNext: smoke-test with `python3 mint-token.py` (should print a fresh access token).")


if __name__ == "__main__":
    main()
