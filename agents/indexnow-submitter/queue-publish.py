#!/usr/bin/env python3
"""Drop a freshly-published URL into the IndexNow force-submit queue.

Called by the implementer (or any agent that publishes a new page) so the
next IndexNow tick — 15 minutes max — pushes that URL to Bing/Yandex
without waiting for the periodic sitemap diff to discover it.

Usage:
    queue-publish.py --site specpicks --url https://specpicks.com/reviews/foo
    queue-publish.py --site specpicks --urls-file /tmp/new-slugs.txt
    queue-publish.py --site aisleprompt --slug recipes/foo-123

This is the cheap-and-fast path the implementer should call AT the
"published_at = NOW()" moment. The IndexNow cron tick then picks up the
queue contents alongside its normal incremental URL discovery.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Iterable


def _queue_root() -> Path:
    """Resolve the force-submit queue dir the indexnow-submitter agent reads.

    Callers of this script (notably the implementer) frequently run with HOME
    pointed at a claude-pool profile — ``<fleet-root>/claude-pool/profile-N``.
    Expanding ``~/.reusable-agents`` there yields a *shadow* queue tree that the
    agent never reads, so every queued URL is silently stranded and the page
    waits for the next sitemap diff instead of being pushed within 15 minutes.
    When HOME is nested inside a ``.reusable-agents`` root, walk back up to it.
    """
    override = os.environ.get("INDEXNOW_QUEUE_ROOT")
    if override:
        return Path(os.path.expanduser(override))
    home = Path(os.path.expanduser("~")).resolve()
    for parent in home.parents:
        if parent.name == ".reusable-agents":
            return parent / "indexnow-submitter"
    return home / ".reusable-agents" / "indexnow-submitter"


QUEUE_ROOT = _queue_root()


def _normalize(site: str, line: str) -> str | None:
    line = line.strip()
    if not line:
        return None
    if line.startswith("http://") or line.startswith("https://"):
        return line
    # Treat as a path/slug — prepend the per-site hostname (loaded from
    # sites.json adjacent to this script).
    here = Path(__file__).resolve().parent
    try:
        import json
        data = json.loads((here / "sites.json").read_text())
        for entry in (data.get("sites") if isinstance(data, dict) else data) or []:
            if isinstance(entry, dict) and entry.get("name") == site:
                host = entry.get("host", "")
                if host:
                    return f"https://{host}/{line.lstrip('/')}"
    except Exception:
        pass
    return None


def queue(site: str, urls: Iterable[str]) -> int:
    QUEUE_ROOT.mkdir(parents=True, exist_ok=True)
    path = QUEUE_ROOT / f"{site}.force-submit.txt"
    existing = path.read_text().splitlines() if path.exists() else []
    existing_set = set(existing)
    added = 0
    for raw in urls:
        u = _normalize(site, raw)
        if u and u not in existing_set:
            existing.append(u)
            existing_set.add(u)
            added += 1
    if added:
        # Trailing newline is load-bearing: operators and other agents append to
        # this queue with ">>", which would otherwise splice a URL onto the last
        # line and make both unusable.
        path.write_text("\n".join(existing[-50000:]) + "\n")
    return added


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", required=True, help="site name (matches sites.json[].name)")
    ap.add_argument("--url", action="append", default=[], help="full URL to queue (repeatable)")
    ap.add_argument("--slug", action="append", default=[], help="site-relative path/slug (repeatable)")
    ap.add_argument("--urls-file", help="file with one URL or slug per line")
    args = ap.parse_args()

    pool: list[str] = list(args.url) + list(args.slug)
    if args.urls_file and os.path.isfile(args.urls_file):
        pool.extend(open(args.urls_file).read().splitlines())

    if not pool:
        ap.print_help()
        return 2

    added = queue(args.site, pool)
    print(f"queued {added} new URL(s) for site={args.site} "
          f"({len(pool) - added} duplicates skipped)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
