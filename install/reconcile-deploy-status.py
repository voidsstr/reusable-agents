#!/usr/bin/env python3
"""reconcile-deploy-status — fix deploy.json blobs whose status=failure
but whose tag actually matches the live container app's active revision.

Cause: Azure occasionally returns a CLI timeout / 5xx after `az
containerapp update` has already accepted the revision swap. The
deployer used to mark such runs as `status: failure` permanently —
even though the recs were on production. The fix landed in
`agents/deployer/deployer.py` (post-flight active-revision check), but
this script back-fills already-shipped chains that pre-date the fix.

What it does:
  1. List all `agents/<source-agent>/runs/<run-ts>/deploy.json` blobs.
  2. For each with status=failure AND a non-empty tag, ask
     `az containerapp revision list` what's currently active for the
     site's app/rg pair (read from site config or deploy_vars).
  3. If active revision's image ends with `:{deploy.tag}`, rewrite
     deploy.json with status=success + verified_via_revision=true.

Idempotent — re-runs are no-ops once everything's in sync.

Usage:
  PYTHONPATH=. python3 install/reconcile-deploy-status.py
  PYTHONPATH=. python3 install/reconcile-deploy-status.py --dry-run
  PYTHONPATH=. python3 install/reconcile-deploy-status.py --site specpicks
  PYTHONPATH=. python3 install/reconcile-deploy-status.py --since 2026-05-01
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
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from framework.core.storage import get_storage  # noqa: E402

# Per-site app/rg lookup. Source of truth is each site's deployer block
# in site.yaml; we hardcode the well-known pairs here for the back-fill
# pass since the storage backend doesn't carry site.yaml itself.
SITE_APP_RG = {
    "aisleprompt": ("aisleprompt", "nsc-apps"),
    "specpicks":   ("specpicks",   "nsc-apps"),
}


def active_image(app: str, rg: str) -> str:
    """Return the image string of the currently-active revision, or ''."""
    try:
        r = subprocess.run(
            ["az", "containerapp", "revision", "list",
             "-g", rg, "-n", app,
             "--query",
             "[?properties.active]|[0].properties.template.containers[0].image",
             "-o", "tsv"],
            capture_output=True, text=True, timeout=60,
        )
        return (r.stdout or "").strip()
    except Exception as e:
        print(f"  [warn] az lookup failed for {app}/{rg}: {e}", file=sys.stderr)
        return ""


def parse_run_ts(run_ts: str) -> datetime | None:
    """Convert a run-ts string (e.g. 20260506T031158Z) to a datetime."""
    try:
        return datetime.strptime(run_ts, "%Y%m%dT%H%M%SZ").replace(
            tzinfo=timezone.utc)
    except Exception:
        return None


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true",
                   help="Report what would be reconciled, don't write blobs")
    p.add_argument("--site", default="",
                   help="Only reconcile this site (e.g. specpicks)")
    p.add_argument("--since", default="",
                   help="Only reconcile runs newer than this ISO date "
                        "(e.g. 2026-05-01)")
    args = p.parse_args()

    since_dt: datetime | None = None
    if args.since:
        try:
            since_dt = datetime.fromisoformat(args.since).replace(
                tzinfo=timezone.utc)
        except Exception as e:
            print(f"--since invalid: {e}", file=sys.stderr)
            return 2

    s = get_storage()
    # Per-agent listing — `agents/` in a mature system holds >10k blobs
    # which trips the storage backend's list-page cap (10k for Azure
    # Blob's flat listing). Discovering the agent set via
    # list_prefix("agents/") silently truncates AT the alphabetic
    # boundary (anything after `aisleprompt-seo-...` is missing). We
    # source the canonical agent list from the framework API which
    # joins multiple registry sources and dedupes — a single HTTP call
    # that returns every dispatching agent regardless of storage layout.
    agent_ids: set[str] = set()
    api = os.environ.get("FRAMEWORK_API_URL",
                          "https://agents.happysky-24190067.eastus.azurecontainerapps.io")
    token = os.environ.get("FRAMEWORK_API_TOKEN", "")
    if api and token:
        try:
            import urllib.request
            req = urllib.request.Request(
                f"{api}/api/agents",
                headers={"Authorization": f"Bearer {token}"},
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                payload = json.loads(resp.read())
            agents = payload.get("agents", payload) if isinstance(payload, dict) else payload
            agent_ids = {a.get("id") for a in (agents or []) if a.get("id")}
        except Exception as e:
            print(f"  [warn] /api/agents fetch failed ({e}); falling back to "
                  f"list_prefix (will be truncated at 10k keys)", file=sys.stderr)
    # Fallback: if API isn't reachable, fall through to the truncating
    # listing so the script still does SOMETHING useful in dev/test.
    if not agent_ids:
        for k in s.list_prefix("agents/"):
            parts = k.split("/")
            if len(parts) >= 2 and parts[0] == "agents":
                agent_ids.add(parts[1])
    keys: list[str] = []
    for aid in sorted(agent_ids):
        for k in s.list_prefix(f"agents/{aid}/runs/"):
            if k.endswith("/deploy.json"):
                keys.append(k)
    print(f"found {len(keys)} deploy.json blobs across {len(agent_ids)} agents",
          file=sys.stderr)

    fixed = 0
    skipped_already_ok = 0
    skipped_no_tag = 0
    skipped_no_match = 0
    skipped_filtered = 0
    cache: dict[tuple[str, str], str] = {}

    for k in keys:
        try:
            d = s.read_json(k) or {}
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        if d.get("status") != "failure":
            skipped_already_ok += 1
            continue
        tag = (d.get("tag") or "").strip()
        if not tag:
            skipped_no_tag += 1
            continue
        site = (d.get("site") or "").strip()
        if not site:
            # Path-derived fallback: agents/<src>/runs/<ts>/deploy.json →
            # source agent name often ends with -<site>-suffix; not robust,
            # but lets us still reconcile when site is missing.
            parts = k.split("/")
            if len(parts) >= 2 and "-" in parts[1]:
                guess = parts[1].split("-", 1)[0]
                if guess in SITE_APP_RG:
                    site = guess
        if args.site and site != args.site:
            skipped_filtered += 1
            continue
        # Filter by --since on the run-ts segment of the path.
        if since_dt is not None:
            run_ts = k.split("/")[3] if len(k.split("/")) > 3 else ""
            ts_dt = parse_run_ts(run_ts)
            if ts_dt and ts_dt < since_dt:
                skipped_filtered += 1
                continue
        app_rg = SITE_APP_RG.get(site)
        if not app_rg:
            print(f"  [warn] {k}: unknown site={site!r}, skipping",
                  file=sys.stderr)
            skipped_no_match += 1
            continue
        app, rg = app_rg
        if app_rg not in cache:
            cache[app_rg] = active_image(app, rg)
        active = cache[app_rg]
        if not active:
            skipped_no_match += 1
            continue
        # Two ways to consider this deploy "shipped":
        #   1. Active revision's image tag literally matches our tag —
        #      the cleanest case, our exact image is live.
        #   2. Active revision's image tag is LEXICOGRAPHICALLY >= our
        #      tag (format is `YYYYMMDD-HHMM`, naturally date-sortable).
        #      A later deploy must have been built from master AT OR
        #      AFTER our commit, since the framework auto-tags by wall
        #      clock and the implementer commits before the deployer
        #      builds. So our recs are present in the live image even
        #      though our specific tag is no longer the current one.
        active_tag = ""
        if ":" in active:
            active_tag = active.rsplit(":", 1)[1]
        is_live = (active.endswith(f":{tag}")
                   or (active_tag >= tag if active_tag and tag else False))
        if not is_live:
            skipped_no_match += 1
            continue
        # MATCH — this deploy actually shipped (or was superseded by a
        # later deploy that includes the same commit). Rewrite the blob.
        d["status"] = "success"
        # Preserve the original deploy stage's rc + stderr for forensics
        # but flag the reconciliation so an operator can tell the success
        # came from a post-hoc check, not a clean run.
        deploy_stage = d.get("deploy") or {}
        if isinstance(deploy_stage, dict):
            deploy_stage["verified_via_revision"] = True
            deploy_stage["reconciled_at"] = datetime.now(timezone.utc).isoformat(
                timespec="seconds").replace("+00:00", "Z")
            deploy_stage["reconciled_active_image"] = active
            d["deploy"] = deploy_stage
        # Mark how we matched so an operator reading the blob can tell
        # whether this is an exact-tag confirmation or a "superseded
        # by a later deploy" reconciliation.
        match_kind = ("exact-tag" if active.endswith(f":{tag}")
                      else "superseded-by-newer-tag")
        if args.dry_run:
            print(f"  [DRY] would reconcile {k} "
                  f"(tag={tag} active={active_tag} match={match_kind})")
        else:
            s.write_json(k, d)
            print(f"  ✓ reconciled {k} "
                  f"(tag={tag} active={active_tag} match={match_kind})")
        fixed += 1

    print(f"\nreconciled={fixed}  already-ok={skipped_already_ok}  "
          f"no-tag={skipped_no_tag}  no-match={skipped_no_match}  "
          f"filtered={skipped_filtered}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
