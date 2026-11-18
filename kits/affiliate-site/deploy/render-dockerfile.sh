#!/usr/bin/env bash
# render-dockerfile.sh: render the kit's Dockerfile.azure.tmpl with a site's deploy.env.
#
#   bash vendor/affiliate-kit/deploy/render-dockerfile.sh --env deploy.env            # print it
#   bash vendor/affiliate-kit/deploy/render-dockerfile.sh --env deploy.env --write    # SITE_ROOT/DOCKERFILE
#   bash vendor/affiliate-kit/deploy/render-dockerfile.sh --env deploy.env --out FILE
#   bash vendor/affiliate-kit/deploy/render-dockerfile.sh --env deploy.env --check    # exit 3 on drift
#   --dry-run with --write / --out: print what would be written (and whether it
#   differs from the file there now) without writing anything
#
# The values come from deploy.env: APP_NAME, NODE_IMAGE, TARGET_PORT,
# SERVER_ENTRY, BUILD_SCRIPT, HEALTH_PATH, DOCKER_EXTRA_PATHS. Commit the
# rendered file: the deploy gate and both clouds build exactly that file, and
# deploy.sh refuses to build a rendered Dockerfile that no longer matches,
# so re-render (--write) and commit whenever deploy.env or the vendored kit
# changes: the kit version is part of the rendered file.
#
# Exit: 0 ok; 2 usage/config error; 3 --check found drift.
set -euo pipefail
# shellcheck source=lib/common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/lib/common.sh"

mode=print
out=""
kd_common_args "$@"
set -- "${KD_REST[@]}"
while (($#)); do
  case $1 in
    --write) mode="write"; shift ;;
    --check) mode="check"; shift ;;
    --out) (($# >= 2)) || kd_die "--out needs a file" 2; mode=out; out=$2; shift 2 ;;
    --out=*) mode=out; out=${1#--out=}; shift ;;
    -h|--help) kd_usage "${BASH_SOURCE[0]}"; exit 0 ;;
    *) kd_die "unknown argument: $1 (see --help)" 2 ;;
  esac
done
kd_load_config

if ((KD_DRY_RUN)) && [[ $mode == write || $mode == out ]]; then
  target=$SITE_ROOT/$DOCKERFILE
  [[ $mode == write ]] || target=$out
  if [[ $target == - ]]; then
    kd_render_dockerfile --out -
    exit 0
  fi
  state="does not exist yet"
  if [[ -f $target ]]; then
    state="differs from this rendering"
    kd_render_dockerfile --check "$target" >/dev/null 2>&1 && state="already matches this rendering"
  fi
  printf '[dry-run] (file %s, %s; not written)\n' "$target" "$state"
  kd_render_dockerfile --out -
  exit 0
fi

case $mode in
  print) kd_render_dockerfile --out - ;;
  write) kd_render_dockerfile --out "$SITE_ROOT/$DOCKERFILE"; kd_log "wrote $SITE_ROOT/$DOCKERFILE" ;;
  out) kd_render_dockerfile --out "$out"; [[ $out == - ]] || kd_log "wrote $out" ;;
  check)
    rc=0
    kd_render_dockerfile --check "$SITE_ROOT/$DOCKERFILE" || rc=$?
    exit "$rc"
    ;;
esac
