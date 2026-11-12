"""CLI entry: reconcile a site repo's untracked migrations with the
database's applied-migrations table (framework/core/migration_ledger.py).

The implementer calls this at the end of every run on a site repo. It is
off unless storage config/migration-ledger-config.json enables the site,
it reads the database in a read-only session, and it never fails the
caller: problems are printed and the exit status is 0 (use --strict to get
a non-zero status for a git or database error).

Usage:
    python3 -m framework.cli.migration_ledger --repo <site repo> --site <site> \\
        [--dsn-env DATABASE_URL] [--dry-run] [--json] [--strict]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from framework.core import migration_ledger as ml


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Commit applied-but-untracked migrations; "
                                            "report or quarantine stranded ones")
    p.add_argument("--repo", required=True)
    p.add_argument("--site", required=True)
    p.add_argument("--dsn-env", default="",
                   help="env var holding the site database DSN (read-only use); default "
                        "DATABASE_URL_<SITE>, then DATABASE_URL")
    p.add_argument("--dry-run", action="store_true", help="plan only, change nothing")
    p.add_argument("--json", action="store_true", help="print the summary as JSON")
    p.add_argument("--strict", action="store_true", help="exit 1 on any error")
    p.add_argument("--force-enable", action="store_true",
                   help="run even when the site is not enabled in config (use with --dry-run)")
    a = p.parse_args(argv)

    cfg = ml.load_config(a.site)
    if not (cfg.get("enabled") or a.force_enable):
        print("migration ledger: disabled for %s (config/migration-ledger-config.json)" % a.site)
        return 0
    repo = ml.repo_root(a.repo)
    if not repo:
        print("migration ledger: %s is not a git checkout" % a.repo)
        return 1 if a.strict else 0
    # The site's own DSN first: a DATABASE_URL inherited from the caller's
    # environment may belong to another site, and reconcile() would then
    # see every applied file as stranded (it refuses when the table does not
    # match the repo, but the right DSN avoids asking).
    names = [a.dsn_env] if a.dsn_env else [
        "DATABASE_URL_" + "".join(c if c.isalnum() else "_" for c in a.site.upper()),
        "DATABASE_URL"]
    env = next((n for n in names if os.environ.get(n)), "")
    dsn = os.environ.get(env, "") if env else ""
    if not dsn:
        print("migration ledger: none of $%s is set; nothing checked" % ", $".join(names))
        return 1 if a.strict else 0
    try:
        applied = ml.applied_names(dsn, str(cfg.get("applied_sql") or ml.DEFAULTS["applied_sql"]),
                                   with_times=True)
    except Exception as e:  # noqa: BLE001
        print("migration ledger: applied-migrations query failed: %s" % str(e)[:200])
        return 1 if a.strict else 0
    summary = ml.reconcile(repo, a.site, applied, cfg, dry_run=a.dry_run)
    if a.json:
        print(json.dumps(summary, indent=2, default=str))
    else:
        pl = summary.get("plan") or {}
        print("migration ledger %s: commit=%d quarantine=%d applied_uncommitted=%d "
              "edited_after_apply=%d stranded=%d young=%d%s" % (
                  a.site, len(pl.get("commit", [])), len(pl.get("quarantine", [])),
                  len(pl.get("applied_uncommitted", [])), len(pl.get("edited_after_apply", [])),
                  len(pl.get("stranded", [])), len(pl.get("young", [])),
                  " (dry run)" if a.dry_run else ""))
        if summary.get("commit_sha"):
            print("  committed %s: %s" % (summary["commit_sha"][:10], ", ".join(pl["commit"])))
        for q in summary.get("quarantined") or []:
            print("  quarantined %s -> %s" % (q["name"], q["to"]))
        for n in pl.get("edited_after_apply", [])[:20]:
            print("  edited after it was applied, NOT committed (reconcile by hand): %s" % n)
        for n in pl.get("stranded", [])[:20]:
            print("  stranded (untracked, not applied): %s" % n)
        for err in summary.get("errors") or []:
            print("  error: %s" % err)
    return 1 if (a.strict and summary.get("errors")) else 0


if __name__ == "__main__":
    sys.exit(main())
