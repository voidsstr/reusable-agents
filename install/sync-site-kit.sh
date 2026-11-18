#!/usr/bin/env bash
# sync-site-kit.sh: vendor the affiliate-site kit into a site repo.
#
#   bash install/sync-site-kit.sh <site-dir> [options]
#
# Copies kits/affiliate-site (the kit's shipped files: no node_modules or
# build output, none of the kit's own unit tests or its gate self-test
# fixture) into <site-dir>/vendor/affiliate-kit, replacing what was there, and
# writes two files next to it:
#   KIT_VERSION      the kit version (the kit's VERSION)
#   MANIFEST.sha256  sha256 of every vendored file, sorted by path, in the
#                    format `sha256sum -c MANIFEST.sha256` reads
# vendor/affiliate-kit is read-only in a site. The site's deploy gate
# (tests/run-local-suite.sh, before every run) and its
# tests/unit/kit-integrity.test.ts check the vendored copy against the
# manifest (a vendored file replaced by a symbolic link counts as an edit),
# then run --verify-anchored from the reusable-agents checkout: the manifest
# lives in the site, so an edit can rewrite it, but not this checkout's
# commits. Kit changes are made in reusable-agents (kits/affiliate-site),
# released with a version bump and committed, and brought in with this script.
#
# The new copy is assembled beside the old one and swapped in with renames,
# so a failure leaves the previous copy in place. After a sync the script
# reports (it does not change) what else the site must follow: kit
# dependency pins its package.json does not carry, and a Dockerfile.azure
# rendered from another kit version.
#
# Options:
#   --kit DIR            the kit to vendor (default: kits/affiliate-site next to this script)
#   --check              change nothing; exit 1 unless the vendored copy is intact,
#                        identical to the kit, and the site follows its pins and template
#   --verify             change nothing; exit 1 unless the vendored copy matches its
#                        own MANIFEST.sha256 (no kit needed; an edit that also
#                        rewrote the manifest passes)
#   --verify-anchored    change nothing; exit 0 only if the vendored copy matches its
#                        manifest AND is byte-identical to the kit version it claims
#                        (KIT_VERSION) as this checkout has it: the kit at a commit
#                        whose VERSION is that version, or the working tree (with a
#                        warning). Exit 1 if it matches no commit of a version that
#                        has commits, or claims a version older than the newest
#                        committed one that no commit holds; exit 4 (a warning, the
#                        gate goes on) if there is nothing to compare: that version
#                        is in no commit here yet and the working tree differs
#   --dry-run            print what a sync would add, remove and update; change nothing
#   --allow-downgrade    vendor an older kit than the site has
#   --json               print a JSON summary of the sync on stdout
#   --quiet              only warnings and errors
#   -h, --help           this text
#
# Upgrade path (kits/affiliate-site/README.md has it in full):
#   bash install/sync-site-kit.sh <site>                      # vendor the new kit, committed here first
#   bash install/sync-site-kit.sh <site> --verify-anchored    # what the site's gate runs: exit 0
#   (cd <site> && npm install <pkg>@<pin> --save-exact ...)    # only if the pins changed
#   bash vendor/affiliate-kit/deploy/render-dockerfile.sh --env deploy.env --write   # in the site
#   npm ci && npm run typecheck && npm test && bash tests/run-local-suite.sh
#   git add -A && git commit -m "Sync affiliate-kit <version>"  # on main
#
# Exit: 0 done (or in sync); 1 out of date, tampered, or a step failed;
#       2 usage or configuration error; 3 a required tool is missing;
#       4 (--verify-anchored) nothing to anchor the vendored copy to.
set -euo pipefail

SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/$(basename "${BASH_SOURCE[0]}")"
REPO_ROOT="$(cd "$(dirname "$SELF")/.." && pwd -P)"
TOOL="$REPO_ROOT/_template/affiliate-site/scaffold/vendor_kit.py"

usage() { sed -n '2,/^set -euo/{/^#/{s/^# \{0,1\}//;p;};}' "$SELF"; }
die() { printf '[sync-site-kit] error: %s\n' "$1" >&2; exit "${2:-1}"; }

SITE=""
KIT="$REPO_ROOT/kits/affiliate-site"
MODE=vendor
declare -a EXTRA=()
set_mode() {
    [[ $MODE == vendor || $MODE == "$1" ]] || die "give one of --check, --verify and --verify-anchored" 2
    MODE=$1
}
while (($#)); do
    case $1 in
        --kit) (($# >= 2)) || die "--kit needs a directory" 2; KIT=$2; shift 2 ;;
        --kit=*) KIT=${1#--kit=}; shift ;;
        --check) set_mode check; shift ;;
        --verify) set_mode verify; shift ;;
        --verify-anchored) set_mode anchor; shift ;;
        --dry-run) EXTRA+=(--dry-run); shift ;;
        --allow-downgrade) EXTRA+=(--allow-downgrade); shift ;;
        --json) EXTRA+=(--json); shift ;;
        --quiet) EXTRA+=(--quiet); shift ;;
        -h|--help) usage; exit 0 ;;
        -*) die "unknown option $1 (see --help)" 2 ;;
        *)
            [[ -z $SITE ]] || die "one site directory at a time (got '$SITE' and '$1')" 2
            SITE=$1; shift ;;
    esac
done
[[ -n $SITE ]] || { usage >&2; exit 2; }
command -v python3 >/dev/null 2>&1 || die "python3 is required" 3
[[ -f $TOOL ]] || die "the scaffold tooling is missing: $TOOL" 2
# --verify-anchored reports a missing kit itself (exit 4: nothing to anchor to).
[[ -d $KIT || $MODE == verify || $MODE == anchor ]] || die "kit directory not found: $KIT" 2

case $MODE in
    vendor)
        exec python3 -I "$TOOL" vendor --kit "$KIT" --site "$SITE" "${EXTRA[@]+"${EXTRA[@]}"}" ;;
    check)
        for o in "${EXTRA[@]+"${EXTRA[@]}"}"; do
            [[ $o == --quiet ]] || die "--check takes only --kit and --quiet" 2
        done
        exec python3 -I "$TOOL" check --kit "$KIT" --site "$SITE" "${EXTRA[@]+"${EXTRA[@]}"}" ;;
    verify)
        for o in "${EXTRA[@]+"${EXTRA[@]}"}"; do
            [[ $o == --quiet ]] || die "--verify takes only --quiet" 2
        done
        exec python3 -I "$TOOL" verify --site "$SITE" "${EXTRA[@]+"${EXTRA[@]}"}" ;;
    anchor)
        for o in "${EXTRA[@]+"${EXTRA[@]}"}"; do
            [[ $o == --quiet ]] || die "--verify-anchored takes only --kit and --quiet" 2
        done
        exec python3 -I "$TOOL" anchor --kit "$KIT" --site "$SITE" "${EXTRA[@]+"${EXTRA[@]}"}" ;;
esac
