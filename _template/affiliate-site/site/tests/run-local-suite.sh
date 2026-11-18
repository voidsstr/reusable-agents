#!/usr/bin/env bash
# This site's deploy gate: the kit runner with this site's tests/suite.env.
#   bash tests/run-local-suite.sh [PLAYWRIGHT ARGS...]      (the deployer runs it from tests/ with --workers=N)
#   KEEP_UP=1 bash tests/run-local-suite.sh                  (leave the app under test running)
# Exit codes, settings and what the gate checks: vendor/affiliate-kit/tests/README.md.
#
# First, the read-only kit boundary, in two steps; a failed step stops the
# gate with exit 78 before anything is built:
#  1. vendor/affiliate-kit must match its MANIFEST.sha256 exactly (every
#     listed file a regular file with its digest, nothing added, no symbolic
#     link).
#  2. The anchor: the copy must be byte-identical to the kit version it
#     claims as the reusable-agents checkout has it, at a commit or in its
#     working tree (that checkout's install/sync-site-kit.sh
#     --verify-anchored). The manifest lives in this repository, so an edit
#     can rewrite it and pass step 1; it cannot rewrite that checkout's
#     commits. The checkout is the one tests/ui/gate.config.ts uses:
#     KIT_GATE_FRAMEWORK_DIR, else REUSABLE_AGENTS_DIR, else reusable-agents
#     next to GATE_SOURCE_REPO (the deployer's live checkout) or next to this
#     repository. With no checkout there, or no commit there holding that kit
#     version yet, the gate prints a warning and goes on;
#     KIT_ANCHOR_REQUIRED=1 makes that exit 78 too.
# The fix is never here or in the manifest: change the kit in reusable-agents,
# commit it, and re-sync (install/sync-site-kit.sh). Operator-owned: the
# implementer scope excludes this file and deploy repairs may not edit it.
#
# What this cannot do: this file is in the repository it checks. A session
# that rewrites it too, or claims a kit version no commit holds yet (step 2
# only warns then), gets an edited kit through. On the implementer's claude
# path no path scope applies (reusable-agents agents/implementer/README.md,
# "Path scope"), so there the kit, site.json, the niche policy,
# tests/ui/gate.config.ts and this file are protected only by instructions.
# A check a site cannot edit would be the deployer running --verify-anchored
# from its own checkout before the gate; it does not do that yet
# (reusable-agents kits/affiliate-site/CHANGELOG.md, Known issues).
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
site_root="$(cd "$here/.." && pwd -P)"
kit="$here/../vendor/affiliate-kit"
if ! command -v python3 >/dev/null 2>&1; then
    printf '[suite] config error: python3 is required to verify vendor/affiliate-kit\n' >&2
    exit 78
fi
if ! python3 -I - "$kit" <<'PY'
import hashlib, os, re, stat, sys

kit = sys.argv[1]
MANIFEST = "MANIFEST.sha256"
LINE = re.compile(r"^([0-9a-f]{64})  (\S(?:.*\S)?)$")
problems = []


def kind(mode):
    return "symbolic link" if stat.S_ISLNK(mode) else "not a regular file"


for d in (os.path.dirname(kit), kit):
    if os.path.islink(d):
        print(f"[suite] {os.path.relpath(d, os.path.dirname(os.path.dirname(kit)))} is a symbolic link: vendor the kit as real files", file=sys.stderr)
        sys.exit(1)
try:
    st = os.lstat(os.path.join(kit, MANIFEST))
    if not stat.S_ISREG(st.st_mode):
        raise OSError(f"{MANIFEST} is a {kind(st.st_mode)}")
    with open(os.path.join(kit, MANIFEST), encoding="utf-8") as fh:
        lines = fh.read().splitlines()
except (OSError, UnicodeDecodeError) as exc:
    print(f"[suite] vendor/affiliate-kit: cannot read {MANIFEST}: {exc}", file=sys.stderr)
    sys.exit(1)

listed = {}
for n, line in enumerate(lines, 1):
    m = LINE.match(line)
    rel = m.group(2) if m else ""
    if not m or rel in listed or rel == MANIFEST or rel.startswith("/") or ".." in rel.split("/"):
        problems.append(f"{MANIFEST} line {n} is not a '<sha256>  <path>' line for a new path")
        continue
    listed[rel] = m.group(1)
if len(listed) < 10:
    problems.append(f"{MANIFEST} lists {len(listed)} files: not a vendored kit")

on_disk = {}
for dirpath, dirnames, filenames in os.walk(kit, followlinks=False):
    for name in sorted(dirnames + filenames):
        full = os.path.join(dirpath, name)
        rel = os.path.relpath(full, kit).replace(os.sep, "/")
        mode = os.lstat(full).st_mode
        if stat.S_ISDIR(mode):
            continue
        if rel != MANIFEST:
            on_disk[rel] = mode
    dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]

for rel, digest in sorted(listed.items()):
    mode = on_disk.get(rel)
    if mode is None:
        problems.append(f"missing: {rel}")
    elif not stat.S_ISREG(mode):
        problems.append(f"modified: {rel} ({kind(mode)})")
    else:
        h = hashlib.sha256()
        with open(os.path.join(kit, rel), "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 16), b""):
                h.update(chunk)
        if h.hexdigest() != digest:
            problems.append(f"modified: {rel}")
for rel in sorted(set(on_disk) - set(listed)):
    mode = on_disk[rel]
    problems.append(f"not in the manifest: {rel}" + ("" if stat.S_ISREG(mode) else f" ({kind(mode)})"))

if problems:
    print("[suite] vendor/affiliate-kit does not match its MANIFEST.sha256 (the kit is read-only in a site):", file=sys.stderr)
    for p in problems[:30]:
        print(f"[suite]   {p}", file=sys.stderr)
    if len(problems) > 30:
        print(f"[suite]   ... and {len(problems) - 30} more", file=sys.stderr)
    sys.exit(1)
try:
    with open(os.path.join(kit, "KIT_VERSION"), encoding="utf-8") as fh:
        version = fh.read().strip()
except OSError:
    version = "?"
print(f"[suite] vendor/affiliate-kit: kit {version}, {len(listed)} files match MANIFEST.sha256")
PY
then
    printf '[suite] config error: vendor/affiliate-kit was changed in this tree. Never edit it, its manifest or this check: change the kit in reusable-agents kits/affiliate-site, release it, and re-sync with install/sync-site-kit.sh\n' >&2
    exit 78
fi

# 2. The anchor, from the reusable-agents checkout (resolved as gate.config.ts does).
ra="${KIT_GATE_FRAMEWORK_DIR:-${REUSABLE_AGENTS_DIR:-}}"
if [[ -z $ra ]]; then
    src="${GATE_SOURCE_REPO:-$site_root}"
    ra="$(dirname -- "${src%/}")/reusable-agents"
fi
anchor_rc=127 anchor_out=""
if [[ -f $ra/install/sync-site-kit.sh ]]; then
    anchor_rc=0
    anchor_out="$(bash "$ra/install/sync-site-kit.sh" "$site_root" --verify-anchored 2>&1)" || anchor_rc=$?
fi
relay() {
    local line prefix='[sync-site-kit] '
    [[ -n $anchor_out ]] || return 0
    while IFS= read -r line; do printf '[suite] %s\n' "${line#"$prefix"}"; done <<< "$anchor_out"
}
case $anchor_rc in
    0) relay ;;
    1)
        relay >&2
        printf '[suite] config error: vendor/affiliate-kit is not the kit release it claims (reusable-agents at %s). Never edit it, its manifest or this check: change the kit in reusable-agents kits/affiliate-site, commit it, and re-sync with install/sync-site-kit.sh\n' "$ra" >&2
        exit 78 ;;
    *)
        relay >&2
        case $anchor_rc in
            127) why="no reusable-agents checkout at $ra (set KIT_GATE_FRAMEWORK_DIR to one)" ;;
            4) why="nothing in $ra to compare it with (above)" ;;
            2) why="$ra/install/sync-site-kit.sh has no --verify-anchored, or refused the call (update that checkout)" ;;
            *) why="the check in $ra failed to run (exit $anchor_rc)" ;;
        esac
        printf '[suite] WARNING: vendor/affiliate-kit was NOT anchored to reusable-agents: %s.\n' "$why" >&2
        printf '[suite] WARNING: it was checked only against its own MANIFEST.sha256, which an edit in this repository can rewrite.\n' >&2
        if [[ ${KIT_ANCHOR_REQUIRED:-0} == 1 ]]; then
            printf '[suite] config error: KIT_ANCHOR_REQUIRED=1 and vendor/affiliate-kit could not be anchored\n' >&2
            exit 78
        fi ;;
esac
exec bash "$kit/tests/run-local-suite.sh" --suite-env "${SUITE_ENV:-$here/suite.env}" "$@"
