#!/usr/bin/env python3
"""The fleet's sites, from the reusable-agents site registry, for the scaffold.

    python3 -I fleet_sites.py check-slug    --repo REPO --slug SLUG --dir DIR
    python3 -I fleet_sites.py gate-projects --repo REPO --except SLUG

Reads framework/core/site_registry.py (the registry's built-in defaults,
shared/sites/registry.default.json). Framework storage is never read, so the
scaffold stays offline and gives the same answer on every run; a site that
only the storage override defines is not seen (pass --host-locks to
create-affiliate-site.sh for it).

check-slug
    Refuses (exit 2) a slug that a registered site already uses, enabled or
    not, unless DIR is that site's own repo_path (creating the registered
    site's repository). The slug names the new site's gate project, its
    fixture containers (<slug>_postgres, <slug>_backend_test, which the gate
    runner removes and recreates), its gate lock, its database and roles and
    its Container App, so a second site with a live site's slug would take
    over that site's local gate.

gate-projects
    Prints the gate projects of every enabled registered site except
    --except, space-separated on one line (empty when there are none). A
    site's gate project is its registry name: the lock its gate holds is
    local-suite-<name>.lock (the kit runner and the reference sites' runners
    use that name). A new site's tests/suite.env names them in
    SUITE_HOST_LOCKS, so its gate never overlaps theirs.

Exit: 0 ok; 1 the registry could not be read; 2 usage error or slug refused.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import List, Optional

EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2


class FleetError(Exception):
    def __init__(self, message: str, code: int = EXIT_FAIL) -> None:
        super().__init__(message)
        self.code = code


def load_sites(repo: Path) -> list:
    """Every registered site (disabled ones included), built-in defaults only."""
    if not (repo / "framework" / "core" / "site_registry.py").is_file():
        raise FleetError(f"{repo} has no framework/core/site_registry.py: run from a complete reusable-agents checkout")
    sys.path.insert(0, str(repo))
    # Read-only: leave no __pycache__ behind in the reusable-agents checkout.
    sys.dont_write_bytecode = True
    try:
        from framework.core import site_registry  # noqa: PLC0415 (the repo is only known at run time)
    except Exception as exc:  # an import error is a broken checkout, not a usage error
        raise FleetError(f"cannot import framework.core.site_registry: {type(exc).__name__}: {exc}") from None
    try:
        registry = site_registry.load_registry(use_storage=False, max_age_s=0)
    except Exception as exc:
        raise FleetError(f"the site registry is invalid: {exc}") from None
    return registry.list_sites(include_disabled=True)


def real(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path))


def cmd_check_slug(repo: Path, slug: str, target: str) -> int:
    for site in load_sites(repo):
        if site.name != slug:
            continue
        if real(site.repo_path) == real(target):
            return EXIT_OK
        state = "" if site.enabled else " (disabled)"
        raise FleetError(
            f"slug '{slug}' is the registered site{state} at {site.repo_path} (reusable-agents site registry): "
            "its gate project, fixture containers, lock, database and roles carry that name; give the new site its own slug",
            EXIT_USAGE,
        )
    return EXIT_OK


def cmd_gate_projects(repo: Path, skip: str) -> int:
    names: List[str] = [s.name for s in load_sites(repo) if s.enabled and s.name != skip]
    print(" ".join(names))
    return EXIT_OK


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="fleet_sites.py", description="The fleet's sites, for the affiliate-site scaffold.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("check-slug", help="refuse a slug a registered site already uses")
    p.add_argument("--repo", required=True)
    p.add_argument("--slug", required=True)
    p.add_argument("--dir", required=True, help="where the new site will be created")
    p = sub.add_parser("gate-projects", help="the enabled sites' gate projects, space-separated")
    p.add_argument("--repo", required=True)
    p.add_argument("--except", dest="skip", default="", help="a site to leave out (the new one)")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code else EXIT_OK
    repo = Path(args.repo).resolve()
    try:
        if args.cmd == "check-slug":
            return cmd_check_slug(repo, args.slug, args.dir)
        return cmd_gate_projects(repo, args.skip)
    except FleetError as exc:
        print(f"fleet_sites: {exc}", file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
