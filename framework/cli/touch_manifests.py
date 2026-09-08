"""Keep agent manifests out of Azure's Archive tier (and out of its 365-day delete).

The `agents-runhistory-tier-then-delete` lifecycle rule on nscagentstorage matches
the WHOLE `agents/` prefix — cool 30d, archive 120d, delete 365d. It is meant for
run history, but `agents/<id>/manifest.json` lives under the same prefix and is
live config, not history. Azure lifecycle `prefixMatch` has no wildcard or suffix
exclusion, so the rule cannot be told to skip manifests.

On 2026-09-08 this had already archived 10 of 83 manifests (including `implementer`
and `deployer`). An archived blob cannot be read OR written, so
`registry.get_agent` silently fell back to the registry rollup and every
`register_agent`/`update_agent` raised after writing the rollup — registry
canonical writes were broken fleet-wide and nothing reported it.

Rewriting a manifest with identical content resets its last-modified date, which
resets the lifecycle clock. Run this on a timer well inside the 120-day window and
manifests can never reach Archive, so they can never reach the delete rule either.

Also self-healing: if a manifest is ALREADY archived, the rewrite is impossible,
so we delete the archived blob first and recreate it Hot from the registry rollup
(which stays readable because it is a different, frequently-written blob).

Usage:
    python3 -m framework.cli.touch_manifests [--dry-run]
"""
from __future__ import annotations

import os
import sys


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    dry = "--dry-run" in argv

    from framework.core import registry as reg
    from framework.core.storage import get_storage

    s = get_storage()
    manifests = reg.list_agents(storage=s)
    print(f"touch-manifests: {len(manifests)} agent(s){' (DRY RUN)' if dry else ''}")

    container = _container()
    touched = rehydrated = failed = 0

    for m in manifests:
        key = f"agents/{m.id}/manifest.json"
        doc = reg.get_agent(m.id, storage=s)
        doc = doc.to_dict() if doc is not None else m.to_dict()
        if dry:
            print(f"  would touch {key} (tier={_tier(container, key)})")
            continue
        try:
            s.write_json(key, doc)
            touched += 1
        except Exception:
            # Almost certainly BlobArchived — archived blobs reject writes.
            # Recreate from the rollup copy we already hold in `doc`.
            try:
                if container is not None:
                    container.get_blob_client(key).delete_blob()
                s.write_json(key, doc)
                rehydrated += 1
                print(f"  rehydrated {key} (was archived)")
            except Exception as exc:  # pragma: no cover - operational path
                failed += 1
                print(f"  FAILED {key}: {type(exc).__name__}: {exc}", file=sys.stderr)

    print(f"touch-manifests: touched={touched} rehydrated={rehydrated} failed={failed}")
    return 1 if failed else 0


def _container():
    """Blob container client, or None when storage is not Azure-backed."""
    cs = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
    if not cs:
        return None
    try:
        from azure.storage.blob import BlobServiceClient
    except ImportError:
        return None
    name = os.environ.get("AZURE_STORAGE_CONTAINER", "agents")
    return BlobServiceClient.from_connection_string(cs).get_container_client(name)


def _tier(container, key: str) -> str:
    if container is None:
        return "?"
    try:
        return container.get_blob_client(key).get_blob_properties().blob_tier or "?"
    except Exception:
        return "?"


if __name__ == "__main__":
    raise SystemExit(main())
