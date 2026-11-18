#!/usr/bin/env bash
# predeploy-gate.sh: the gate a manual deploy runs before it builds anything.
#
# Runs the site's deploy-gate suite (the same suite and arguments the agent
# deployer gates on) against the working tree that is about to be built, and
# passes only when the suite passed AND the tree did not change while it ran:
# if HEAD, the diff or an untracked file changed, the image built next would not
# be the code that was tested.
#
#   bash predeploy-gate.sh --site-root DIR [--suite tests/run-local-suite.sh] [--dry-run] [-- SUITE_ARGS...]
#
# --dry-run checks the arguments and the suite, prints the command the gate
# would run, and runs nothing.
#
# azure/deploy.sh and aws/deploy.sh call it with PREDEPLOY_GATE_SUITE and
# PREDEPLOY_GATE_ARGS from deploy.env and skip it only with --skip-tests[=REASON].
# The suite runs from its own directory (the deployer's `cwd: tests`).
# Ported from SpecPicks tests/predeploy-gate.sh.
#
# Exit: 0 passed; 1 the suite failed or the tree changed; 2 usage error.
set -uo pipefail

root=""
suite="tests/run-local-suite.sh"
dry_run=0
args=()
while (($#)); do
  case $1 in
    --site-root) root=${2:-}; shift 2 || { echo "[gate] --site-root needs a directory" >&2; exit 2; } ;;
    --suite) suite=${2:-}; shift 2 || { echo "[gate] --suite needs a path" >&2; exit 2; } ;;
    --dry-run) dry_run=1; shift ;;
    --) shift; args=("$@"); break ;;
    -h|--help) sed -n '2,/^[^#]/{/^#/{s/^# \{0,1\}//;p;};}' "$0"; exit 0 ;;
    *) echo "[gate] unknown argument: $1" >&2; exit 2 ;;
  esac
done
[[ -n $root && -d $root ]] || { echo "[gate] --site-root must be the site repository" >&2; exit 2; }
root=$(cd "$root" && pwd -P)
[[ $suite != /* && $suite != *..* ]] || { echo "[gate] --suite must be a path inside the site" >&2; exit 2; }
if [[ ! -f $root/$suite ]]; then
  echo "[gate] no gate suite at $root/$suite (PREDEPLOY_GATE_SUITE). Untested code does not ship." >&2
  exit 1
fi

tree_state() {
  {
    git -C "$root" rev-parse HEAD
    git -C "$root" diff HEAD
    git -C "$root" ls-files --others --exclude-standard -z | (cd "$root" && xargs -0 -r sha1sum)
  } 2>/dev/null | sha1sum
}

suite_dir=$(dirname "$root/$suite")
if ((dry_run)); then
  echo "[dry-run] [gate] would run: (cd $suite_dir && bash $(basename "$suite") ${args[*]}), then check that the tree did not change"
  exit 0
fi

if ! git -C "$root" rev-parse --git-dir >/dev/null 2>&1; then
  echo "[gate] WARN: $root is not a git repository, so a change during the run cannot be detected" >&2
fi
before=$(tree_state)
echo "[gate] pre-deploy gate: (cd $suite_dir && bash $(basename "$suite") ${args[*]})"
(cd "$suite_dir" && bash "$(basename "$suite")" "${args[@]}")
rc=$?
if ((rc != 0)); then
  echo "[gate] FAILED (exit $rc). Not deploying: fix what the suite reported and run it again."
  exit 1
fi
if [[ $(tree_state) != "$before" ]]; then
  echo "[gate] the working tree changed while the gate ran, so what would be built is not what was tested. Run the deploy again."
  exit 1
fi
echo "[gate] passed"
