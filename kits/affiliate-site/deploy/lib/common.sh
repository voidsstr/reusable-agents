# shellcheck shell=bash
#
# lib/common.sh: shared by the affiliate-site kit deploy scripts
# (../azure/*.sh, ../aws/*.sh, ../render-dockerfile.sh). Source it after
# `set -euo pipefail`; it defines functions and does nothing on its own.
#
#   config    deploy.env is parsed by deploytool env-dump (never `source`d),
#             unknown keys are errors, defaults and validation live here
#   dry run   every cloud, docker and psql command goes through kd_run,
#             kd_probe_*, kd_psql_exec or kd_run_with_secret_env, which print
#             it under --dry-run instead of running it
#   secrets   env:NAME / file:PATH references; values go to the cloud only
#             through 0600 files or one child's environment; kd_guard_args
#             refuses to run (or print) any command line containing a value
#   database  the roles/grants SQL both clouds apply to their Postgres server
#   release   the build, gate, migration and smoke steps both clouds share
#
# Exit codes used by every script: 0 ok; 1 a step failed; 2 usage or config
# error; 3 the operator has to act (DNS records, missing secret, pending
# certificate) and then re-run; 4 (deploy.sh) the smoke check failed;
# 70 internal error (a secret reached a command line).

if ((BASH_VERSINFO[0] < 4 || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] < 4))); then
  echo "affiliate-kit deploy: bash >= 4.4 is required (found ${BASH_VERSION})" >&2
  exit 2
fi

# Under xtrace (bash -x, set -x, SHELLOPTS=xtrace) bash prints every command
# after expansion, secret values included. Refuse rather than leak them;
# --dry-run shows the commands without running them.
if [[ $- == *x* ]]; then
  set +x
  echo "affiliate-kit deploy: refusing to run with xtrace on (bash -x / set -x): it would print secret values. Use --dry-run to see the commands." >&2
  exit 2
fi

KD_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
KD_DEPLOY_DIR="$(cd "$KD_LIB_DIR/.." && pwd -P)"
KD_KIT_DIR="$(cd "$KD_DEPLOY_DIR/.." && pwd -P)"
KD_TOOL="$KD_LIB_DIR/deploytool.py"
KD_PYTHON="${KIT_DEPLOY_PYTHON:-python3}"
KD_DRY_RUN=0
# What existence probes answer under --dry-run when KIT_DEPLOY_DRY_EXISTING /
# KIT_DEPLOY_DRY_MISSING do not name the resource: provision previews the
# create path (absent), deploy and teardown the normal case (present).
KD_DRY_DEFAULT=absent
KD_ENV_ARG=""
KD_ENV_FILE=""
KD_ENV_DIR=""
KD_SECRET_DIR=""
KD_SECRETS_FILE=""
KD_AI_EXCLUDE=""
KD_ENV_AI_TRAFFIC_EXCLUDE_IPS=""
KD_SKIP_TESTS=0
KD_SKIP_TESTS_REASON=""
# deploy.sh --gated-by=WHO: an outer pipeline (the deployer) ran the gate.
KD_GATED_BY=""
# provision.sh --adopt-existing: take over unmarked roles, database and app.
KD_ADOPT=0
# The shortest value accepted as a secret, and so the shortest one the leak
# guard looks for (a shorter "secret" would match ordinary command lines).
KD_MIN_SECRET_LEN=8
# The ownership marker the kit puts on what it creates: a COMMENT on the
# database roles and the database, an affiliate-kit-app tag on cloud resources.
KD_MARKER_PREFIX="affiliate-kit app="
KD_OWNER_TAG="affiliate-kit-app"
# What an existence probe may read as "absent": the not-found errors of az and
# aws. Any other failure (network, auth, throttling) stops the script, so a
# transient error never sends it down a create path over a live resource.
KD_NOT_FOUND_RE='NotFound|[Nn]ot [Ff]ound|could not be found|does not exist|not known to registry|NAME_UNKNOWN|MANIFEST_UNKNOWN'
declare -a KD_SECRET_VALUES=()
declare -a KD_REST=()
declare -a KD_CENV=()
declare -a KD_CSECRET_ENV=()
declare -a KD_SMOKE_PATHS=()
declare -A KD_ROLE_PASSWORD=()
KD_ROLE_KINDS=(OWNER APP AGENTS PUBLISHER)

if [[ -r "$KD_KIT_DIR/VERSION" ]]; then
  KD_KIT_VERSION="$(tr -d '[:space:]' < "$KD_KIT_DIR/VERSION")"
elif [[ -r "$KD_KIT_DIR/KIT_VERSION" ]]; then
  KD_KIT_VERSION="$(tr -d '[:space:]' < "$KD_KIT_DIR/KIT_VERSION")"
else
  KD_KIT_VERSION="unknown"
fi

# Every key deploy.env may set (deploy.env.example documents each one).
KD_KNOWN_KEYS=(
  APP_NAME SITE_DOMAIN SITE_HOSTNAMES SITE_ROOT SITE_JSON TARGET_PORT
  DOCKERFILE NODE_IMAGE SERVER_ENTRY BUILD_SCRIPT HEALTH_PATH DOCKER_EXTRA_PATHS DOCKER_PLATFORM
  CPU MEMORY MIN_REPLICAS MAX_REPLICAS
  SECRETS_FILE APP_ENV APP_SECRETS APP_SECRET_ENV AI_TRAFFIC_EXCLUDE_IPS
  DB_NAME DB_PORT DB_SSLMODE
  DB_OWNER_ROLE DB_APP_ROLE DB_AGENTS_ROLE DB_PUBLISHER_ROLE
  DB_OWNER_DSN DB_APP_DSN DB_AGENTS_DSN DB_PUBLISHER_DSN
  DB_OWNER_CONNECTION_LIMIT DB_APP_CONNECTION_LIMIT DB_AGENTS_CONNECTION_LIMIT DB_PUBLISHER_CONNECTION_LIMIT
  DB_OWNER_STATEMENT_TIMEOUT DB_APP_STATEMENT_TIMEOUT DB_AGENTS_STATEMENT_TIMEOUT DB_PUBLISHER_STATEMENT_TIMEOUT
  DB_IDLE_IN_TRANSACTION_TIMEOUT
  DB_APP_GRANTS DB_AGENTS_GRANTS DB_PUBLISHER_GRANTS
  PREDEPLOY_GATE_SUITE PREDEPLOY_GATE_ARGS MIGRATE_COMMAND
  SMOKE_PATHS SMOKE_TIMEOUT_SECONDS HTML_MAX_S_MAXAGE READY_TIMEOUT_SECONDS
  AZ_SUBSCRIPTION AZ_RESOURCE_GROUP AZ_LOCATION AZ_ENVIRONMENT AZ_ACR AZ_LOG_WORKSPACE
  AZ_PG_SERVER AZ_DB_HOST AZ_DB_ADMIN_USER AZ_DB_ADMIN_PASSWORD
  AWS_STATE_FILE AWS_SECRET_ID AWS_TARGET_GROUP AWS_CPU_TARGET_PERCENT AWS_SSL_POLICY
  AWS_DB_HOST AWS_DB_ADMIN_USER AWS_DB_ADMIN_PASSWORD
  AWS_APP_SECRETS AWS_DB_OWNER_DSN AWS_DB_APP_DSN AWS_DB_AGENTS_DSN AWS_DB_PUBLISHER_DSN
)
declare -A KD_KNOWN=()
for _kd_k in "${KD_KNOWN_KEYS[@]}"; do KD_KNOWN[$_kd_k]=1; done
unset _kd_k

# ─── output ───────────────────────────────────────────────────────────────────

kd_log() { printf '[%s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }
kd_warn() { printf 'WARN: %s\n' "$*" >&2; }
kd_die() {
  printf 'ERROR: %s\n' "$1" >&2
  exit "${2:-1}"
}
kd_phase() { printf '\n== %s ==\n' "$*"; }

# kd_usage SCRIPT: print the script's leading comment block (its --help), up to
# the first non-comment or shellcheck-directive line.
kd_usage() {
  local line n=0
  while IFS= read -r line; do
    n=$((n + 1))
    ((n == 1)) && continue
    [[ $line == '#'* && $line != '# shellcheck'* ]] || break
    line=${line#\#}
    printf '%s\n' "${line# }"
  done < "$1"
}

# One word, shell-quoted only when it needs to be (copy-pasteable output).
kd_q() {
  local s=$1
  if [[ -n $s && $s =~ ^[A-Za-z0-9_@%+=:,./-]+$ ]]; then
    printf '%s' "$s"
  else
    printf "'%s'" "${s//\'/\'\\\'\'}"
  fi
}

kd_qcmd() {
  local out="" w
  for w in "$@"; do out+="${out:+ }$(kd_q "$w")"; done
  printf '%s' "$out"
}

kd_trim() {
  local s=$1
  s="${s#"${s%%[![:space:]]*}"}"
  s="${s%"${s##*[![:space:]]}"}"
  printf '%s' "$s"
}

kd_var() { printf '%s' "${!1-}"; }

# ─── secrets ──────────────────────────────────────────────────────────────────

# kd_register_secret VALUE: remember a secret so kd_guard_args can refuse any
# command line containing it. For a DSN the password inside it is registered
# too (as written and percent-decoded), so a copy of just the password is caught.
kd_register_secret() {
  local v=$1 pw="" url_re='^[A-Za-z][A-Za-z0-9+.-]*://[^/@:]*:([^@/]+)@' kv_re="(^|[[:space:]])password=('([^'\\\\]|\\\\.)*'|[^[:space:]]+)"
  kd__remember_secret "$v"
  if [[ $v =~ $url_re ]]; then
    pw=${BASH_REMATCH[1]}
  elif [[ $v =~ $kv_re ]]; then
    pw=${BASH_REMATCH[2]}
    pw=${pw#\'}
    pw=${pw%\'}
  fi
  if ((${#pw} >= KD_MIN_SECRET_LEN)); then
    kd__remember_secret "$pw"
    [[ $pw == *%* ]] && kd__remember_secret "$(printf '%b' "${pw//%/\\x}")"
  fi
  return 0
}

kd__remember_secret() {
  local s
  ((${#1} >= KD_MIN_SECRET_LEN)) || return 0
  for s in "${KD_SECRET_VALUES[@]}"; do [[ $s != "$1" ]] || return 0; done
  KD_SECRET_VALUES+=("$1")
}

# kd_register_loaded REF VALUE: register a secret just read from REF, refusing
# one too short to be a secret (the leak guard could not tell it from text).
kd_register_loaded() {
  ((${#2} >= KD_MIN_SECRET_LEN)) \
    || kd_die "secret $1 is shorter than $KD_MIN_SECRET_LEN characters: too short to be a secret, and too short for the leak guard to recognise" 2
  kd_register_secret "$2"
}

# kd_redact TEXT: TEXT with every secret value this run has loaded replaced.
kd_redact() {
  local t=$1 s
  for s in "${KD_SECRET_VALUES[@]}"; do t=${t//"$s"/<redacted>}; done
  printf '%s' "$t"
}

# Refuse a command line that contains any secret value this run has loaded.
kd_guard_args() {
  local a s
  for a in "$@"; do
    for s in "${KD_SECRET_VALUES[@]}"; do
      if [[ $a == *"$s"* ]]; then
        kd_die "internal error: a secret value reached a command line; nothing was run" 70
      fi
    done
  done
}

kd_expand_home() {
  local p=$1 tilde="~"
  if [[ $p == "$tilde" ]]; then
    p=$HOME
  elif [[ $p == "$tilde"/* ]]; then
    p="$HOME/${p#\~/}"
  fi
  printf '%s' "$p"
}

# Helpers that assign a caller's variable by name (printf -v) give their own
# locals a prefix unique to the function (_sg_, _sf_, ...), so no caller's
# variable, and no other helper's, is ever shadowed.

# kd_secrets_file_lookup NAME OUTVAR: NAME's value from SECRETS_FILE (not registered).
kd_secrets_file_lookup() {
  local _sl_want=$1 _sl_dst=$2 _sl_k _sl_v _sl_found=1
  [[ -n $KD_SECRETS_FILE && -r $KD_SECRETS_FILE ]] || return 1
  while IFS= read -r -d '' _sl_k && IFS= read -r -d '' _sl_v; do
    if [[ $_sl_k == "$_sl_want" ]]; then
      printf -v "$_sl_dst" '%s' "$_sl_v"
      _sl_found=0
    fi
  done < <("$KD_PYTHON" -I "$KD_TOOL" env-dump "$KD_SECRETS_FILE" --keys "$_sl_want")
  return "$_sl_found"
}

# kd_exported_value NAME OUTVAR: NAME's value when it is an exported variable
# (the caller's environment), never one of this script's own shell variables
# that happens to share the name. Returns 1 when it is not exported or empty.
kd_exported_value() {
  local _ev_name=$1 _ev_dst=$2
  [[ -v $_ev_name ]] || return 1
  local -n _ev_ref=$_ev_name
  [[ ${_ev_ref@a} == *x* && -n $_ev_ref ]] || return 1
  printf -v "$_ev_dst" '%s' "$_ev_ref"
}

# kd_secret_get REF OUTVAR: resolve env:NAME (environment, then SECRETS_FILE) or
# file:PATH. Returns 1 when the secret is missing or empty. Never prints it.
kd_secret_get() {
  local _sg_ref=$1 _sg_dst=$2 _sg_val="" _sg_name _sg_path _sg_mode
  case $_sg_ref in
    env:*)
      _sg_name=${_sg_ref#env:}
      [[ $_sg_name =~ ^[A-Za-z][A-Za-z0-9_]*$ ]] || kd_die "bad secret reference '$_sg_ref'" 2
      kd_exported_value "$_sg_name" _sg_val || kd_secrets_file_lookup "$_sg_name" _sg_val || _sg_val=""
      ;;
    file:*)
      _sg_path=$(kd_expand_home "${_sg_ref#file:}")
      if [[ -f $_sg_path && -r $_sg_path ]]; then
        _sg_mode=$(stat -c %a "$_sg_path" 2>/dev/null || echo 600)
        if (( (8#$_sg_mode & 8#077) != 0 )); then
          kd_warn "$_sg_path (secret file) is readable by group or others; chmod 600 it"
        fi
        _sg_val=$(<"$_sg_path")
      fi
      ;;
    *) kd_die "bad secret reference '$_sg_ref' (use env:NAME or file:PATH)" 2 ;;
  esac
  [[ -n $_sg_val ]] || return 1
  kd_register_loaded "$_sg_ref" "$_sg_val"
  printf -v "$_sg_dst" '%s' "$_sg_val"
}

# kd_register_configured_secrets: read every secret deploy.env refers to, so the
# leak guard knows each value in every script, also in the ones that never use
# it (deploy.sh's release, render-dockerfile.sh). Best effort: a secret that is
# not set is skipped here; the step that needs it says so.
kd_register_configured_secrets() {
  local ref e kind key v p
  local -a refs=() items=() lookup=()
  local -A loaded=()
  read -ra items <<< "$APP_SECRETS $AWS_APP_SECRETS"
  for e in "${items[@]}"; do refs+=("${e#*=}"); done
  for kind in "${KD_ROLE_KINDS[@]}"; do refs+=("$(kd_var "DB_${kind}_DSN")" "$(kd_var "AWS_DB_${kind}_DSN")"); done
  refs+=("$AZ_DB_ADMIN_PASSWORD" "$AWS_DB_ADMIN_PASSWORD")
  for ref in "${refs[@]}"; do
    [[ -n $ref && -z ${loaded[$ref]-} ]] || continue
    loaded[$ref]=1
    v=""
    case $ref in
      env:*)
        key=${ref#env:}
        if kd_exported_value "$key" v; then
          kd_register_loaded "$ref" "$v"
        else
          lookup+=("$key")
        fi
        ;;
      file:*)
        p=$(kd_expand_home "${ref#file:}")
        if [[ -f $p && -r $p ]]; then
          v=$(<"$p")
          [[ -z $v ]] || kd_register_loaded "$ref" "$v"
        fi
        ;;
    esac
  done
  ((${#lookup[@]})) || return 0
  [[ -n $KD_SECRETS_FILE && -r $KD_SECRETS_FILE ]] || return 0
  while IFS= read -r -d '' key && IFS= read -r -d '' v; do
    [[ -z $v ]] || kd_register_loaded "env:$key" "$v"
  done < <(IFS=,; "$KD_PYTHON" -I "$KD_TOOL" env-dump "$KD_SECRETS_FILE" --keys "${lookup[*]}")
}

kd_secret_dir() {
  if [[ -z $KD_SECRET_DIR ]]; then
    local base=${XDG_RUNTIME_DIR:-${TMPDIR:-/tmp}}
    [[ -d $base && -w $base ]] || base=/tmp
    KD_SECRET_DIR=$(umask 077 && mktemp -d "$base/affiliate-kit-deploy.XXXXXX")
  fi
}

# kd_secret_file NAME REF OUTVAR: write the secret to a 0600 file and return its
# path. Under --dry-run nothing is written; a placeholder path is returned and a
# missing secret is only a warning.
kd_secret_file() {
  local _sf_name=$1 _sf_ref=$2 _sf_dst=$3 _sf_val _sf_path
  [[ $_sf_name =~ ^[A-Za-z0-9._-]+$ ]] || kd_die "bad secret name '$_sf_name'" 2
  if ! kd_secret_get "$_sf_ref" _sf_val; then
    if ((KD_DRY_RUN)); then
      kd_warn "secret '$_sf_name' ($_sf_ref) is not set: the real run stops here"
      printf -v "$_sf_dst" '%s' "<secret-dir>/$_sf_name"
      return 0
    fi
    return 1
  fi
  if ((KD_DRY_RUN)); then
    printf -v "$_sf_dst" '%s' "<secret-dir>/$_sf_name"
    return 0
  fi
  kd_secret_dir
  _sf_path="$KD_SECRET_DIR/$_sf_name"
  (umask 077 && printf '%s' "$_sf_val" > "$_sf_path")
  [[ -s $_sf_path ]] || kd_die "could not write the secret file for '$_sf_name'" 1
  printf -v "$_sf_dst" '%s' "$_sf_path"
}

kd_cleanup() {
  if [[ -n $KD_SECRET_DIR && -d $KD_SECRET_DIR ]]; then
    rm -rf -- "$KD_SECRET_DIR"
  fi
  KD_SECRET_DIR=""
}
trap kd_cleanup EXIT
trap 'kd_cleanup; exit 130' INT
trap 'kd_cleanup; exit 143' TERM

# ─── running commands ─────────────────────────────────────────────────────────

kd_need_tool() {
  local t
  for t in "$@"; do
    if ! command -v "$t" >/dev/null 2>&1; then
      if ((KD_DRY_RUN)); then
        kd_warn "$t is not installed here (fine for --dry-run; the real run needs it)"
      else
        kd_die "$t is required but not installed" 2
      fi
    fi
  done
}

# kd_run CMD...: run a command that changes something (or print it under --dry-run).
kd_run() {
  kd_guard_args "$@"
  if ((KD_DRY_RUN)); then
    printf '[dry-run] %s\n' "$(kd_qcmd "$@")"
    return 0
  fi
  kd_log "+ $(kd_qcmd "$@")"
  "$@"
}

kd_dry_listed() {
  local list=",${1// /}," key=$2
  [[ $list == *",$key,"* ]]
}

kd__probe() {
  local _pr_mode=$1 _pr_var=$2 _pr_key=$3 _pr_ans="" _pr_out _pr_rc=0 _pr_errf _pr_err
  shift 3
  kd_guard_args "$@"
  if ((KD_DRY_RUN)); then
    printf '[dry-run] (probe) %s\n' "$(kd_qcmd "$@")"
    if kd_dry_listed "${KIT_DEPLOY_DRY_MISSING:-}" "$_pr_key"; then
      _pr_ans=""
    elif kd_dry_listed "${KIT_DEPLOY_DRY_EXISTING:-}" "$_pr_key"; then
      _pr_ans="<$_pr_key>"
    elif [[ $_pr_mode != exists || $KD_DRY_DEFAULT == present ]]; then
      _pr_ans="<$_pr_key>"
    fi
    printf -v "$_pr_var" '%s' "$_pr_ans"
    return 0
  fi
  kd_secret_dir
  _pr_errf="$KD_SECRET_DIR/probe.stderr"
  _pr_out=$("$@" 2>"$_pr_errf") || _pr_rc=$?
  if ((_pr_rc != 0)); then
    _pr_err=$(<"$_pr_errf")
    _pr_out=""
    case $_pr_mode in
      exists) [[ $_pr_err =~ $KD_NOT_FOUND_RE ]] || kd_probe_failed "$_pr_rc" "$_pr_err" "$@" ;;
      strict) kd_probe_failed "$_pr_rc" "$_pr_err" "$@" ;;
    esac
  fi
  rm -f -- "$_pr_errf"
  _pr_out=$(kd_trim "$_pr_out")
  [[ $_pr_out == None || $_pr_out == null ]] && _pr_out=""
  printf -v "$_pr_var" '%s' "$_pr_out"
}

# kd_probe_failed RC STDERR CMD...: a read-only query failed for a reason other
# than "not found"; stop before anything acts on a guess.
kd_probe_failed() {
  local rc=$1 err=$2
  shift 2
  err=$(kd_redact "$err" | tail -n 5)
  kd_die "a read-only query failed (exit $rc), so the script cannot tell what exists; it stopped before changing anything that depends on the answer. Re-run once the cause is fixed.
  query: $(kd_qcmd "$@")
  ${err:-(no error output)}" 1
}

# kd_dry_created KEY: under --dry-run, later probes of KEY see the resource this
# run just (pretend-)created.
kd_dry_created() {
  ((KD_DRY_RUN)) || return 0
  KIT_DEPLOY_DRY_EXISTING="${KIT_DEPLOY_DRY_EXISTING:-}${KIT_DEPLOY_DRY_EXISTING:+,}$1"
}

# kd_probe_exists VAR KEY CMD...: read-only query whose (non-empty) output means
# the resource exists. A not-found error (KD_NOT_FOUND_RE) means absent; any
# other failure stops the script (exit 1). Under --dry-run: KD_DRY_DEFAULT
# unless KEY is listed in KIT_DEPLOY_DRY_EXISTING / KIT_DEPLOY_DRY_MISSING.
kd_probe_exists() { kd__probe exists "$@"; }

# kd_probe_value VAR KEY CMD...: read-only query for a value the script needs;
# empty when the query fails. Under --dry-run it answers "<KEY>" unless KEY is
# in KIT_DEPLOY_DRY_MISSING.
kd_probe_value() { kd__probe value "$@"; }

# kd_probe_strict VAR KEY CMD...: kd_probe_value for answers a decision rests
# on (ownership tags, the image a rollback needs): any failure stops the script.
kd_probe_strict() { kd__probe strict "$@"; }

# kd_require_owner WHAT TAG [ADOPT_FN]: WHAT exists and TAG is its
# affiliate-kit-app tag (set when the kit created it). This site's: fine. No
# tag, with --adopt-existing and an ADOPT_FN (provision only): ADOPT_FN tags it.
# Anything else stops the script before it changes the resource: deploy.env
# names a resource another site (or something other than the kit) owns.
kd_require_owner() {
  local what=$1 tag=$2 adopt_fn=${3:-}
  if ((KD_DRY_RUN)) && [[ $tag == '<'*'>' ]]; then return 0; fi
  [[ $tag == "$APP_NAME" ]] && return 0
  if [[ -n $tag ]]; then
    kd_die "$what belongs to affiliate-kit app '$tag', not $APP_NAME: check APP_NAME and the resource names in $KD_ENV_FILE" 2
  fi
  if ((KD_ADOPT)) && [[ -n $adopt_fn ]]; then
    kd_warn "$what has no $KD_OWNER_TAG tag; adopting it for $APP_NAME (--adopt-existing)"
    "$adopt_fn"
    return 0
  fi
  kd_die "$what exists but was not created by the kit for $APP_NAME (no $KD_OWNER_TAG tag): check APP_NAME and the resource names in $KD_ENV_FILE${adopt_fn:+. To take over a resource that predates the kit, re-run provision.sh with --adopt-existing}" 2
}

# kd_run_capture VAR KEY CMD...: run a command that changes something and prints
# a value (an ARN); under --dry-run print it and answer "<KEY>".
kd_run_capture() {
  local _rc_var=$1 _rc_key=$2 _rc_out
  shift 2
  kd_guard_args "$@"
  if ((KD_DRY_RUN)); then
    printf '[dry-run] %s\n' "$(kd_qcmd "$@")"
    printf -v "$_rc_var" '%s' "<$_rc_key>"
    return 0
  fi
  kd_log "+ $(kd_qcmd "$@")"
  _rc_out=$("$@") || return 1
  _rc_out=$(kd_trim "$_rc_out")
  [[ $_rc_out == None || $_rc_out == null ]] && _rc_out=""
  printf -v "$_rc_var" '%s' "$_rc_out"
}

# kd_work_file NAME VAR: a path in this run's private (0700) scratch directory.
kd_work_file() {
  if ((KD_DRY_RUN)); then
    printf -v "$2" '%s' "<work-dir>/$1"
    return 0
  fi
  kd_secret_dir
  printf -v "$2" '%s' "$KD_SECRET_DIR/$1"
}

# kd_show_file PATH CONTENT: under --dry-run, show a (secret-free) file the next command reads.
kd_show_file() {
  kd_guard_args "$2"
  printf '[dry-run] (file %s)\n%s\n' "$1" "$2"
}

# kd_run_with_secret_env ENVNAME REF CMD...: run CMD with one secret in its
# environment only (never on its command line).
kd_run_with_secret_env() {
  local envname=$1 ref=$2 value=""
  shift 2
  # Load (and register) the secret first, so the guard also knows this one.
  if ! kd_secret_get "$ref" value; then
    ((KD_DRY_RUN)) || kd_die "secret $ref is not set (environment or $KD_SECRETS_FILE)" 3
    kd_warn "secret $ref is not set: the real run stops here"
  fi
  kd_guard_args "$@"
  if ((KD_DRY_RUN)); then
    printf '[dry-run] %s=<%s> %s\n' "$envname" "$ref" "$(kd_qcmd "$@")"
    return 0
  fi
  kd_log "+ $envname=<$ref> $(kd_qcmd "$@")"
  (
    export "$envname=$value"
    exec "$@"
  )
}

# kd_wait_until DESCRIPTION TIMEOUT_S INTERVAL_S FN: call FN until it succeeds.
# FN returning 2 aborts the wait. Under --dry-run FN runs once (its probes print).
kd_wait_until() {
  local desc=$1 timeout=$2 interval=$3 fn=$4 rc deadline
  if ((KD_DRY_RUN)); then
    printf '[dry-run] (wait up to %ss) %s\n' "$timeout" "$desc"
    "$fn" || true
    return 0
  fi
  deadline=$((SECONDS + timeout))
  while :; do
    rc=0
    "$fn" || rc=$?
    ((rc == 0)) && return 0
    ((rc == 2)) && return 1
    if ((SECONDS >= deadline)); then
      kd_warn "timed out after ${timeout}s: $desc"
      return 1
    fi
    sleep "$interval"
  done
}

# ─── config ───────────────────────────────────────────────────────────────────

# kd_common_args ARGS...: consume --env FILE and --dry-run; the rest -> KD_REST.
kd_common_args() {
  KD_REST=()
  while (($#)); do
    case $1 in
      --env)
        (($# >= 2)) || kd_die "--env needs a file" 2
        KD_ENV_ARG=$2
        shift 2
        ;;
      --env=*) KD_ENV_ARG=${1#--env=}; shift ;;
      --dry-run) KD_DRY_RUN=1; shift ;;
      *) KD_REST+=("$1"); shift ;;
    esac
  done
}

kd_load_config() {
  local file=${1:-$KD_ENV_ARG} k v
  if [[ -z $file ]]; then
    [[ -f ./deploy.env ]] || kd_die "no deploy env file: pass --env <site>/deploy.env" 2
    file=./deploy.env
  fi
  [[ -f $file ]] || kd_die "deploy env file not found: $file" 2
  command -v "$KD_PYTHON" >/dev/null 2>&1 || kd_die "$KD_PYTHON is required" 2
  KD_ENV_FILE="$(cd "$(dirname "$file")" && pwd -P)/$(basename "$file")"
  KD_ENV_DIR="$(dirname "$KD_ENV_FILE")"
  "$KD_PYTHON" -I "$KD_TOOL" env-dump --strict --check "$KD_ENV_FILE" \
    || kd_die "fix $KD_ENV_FILE (deploy.env.example documents every key)" 2
  KD_ENV_AI_TRAFFIC_EXCLUDE_IPS=${AI_TRAFFIC_EXCLUDE_IPS-}
  unset "${KD_KNOWN_KEYS[@]}"
  while IFS= read -r -d '' k && IFS= read -r -d '' v; do
    [[ -n ${KD_KNOWN[$k]-} ]] || kd_die "$KD_ENV_FILE: unknown key $k (deploy.env.example lists every key)" 2
    printf -v "$k" '%s' "$v"
  done < <("$KD_PYTHON" -I "$KD_TOOL" env-dump --strict "$KD_ENV_FILE")
  kd_apply_defaults
  kd_validate_common
  kd_register_configured_secrets
}

kd_apply_defaults() {
  : "${SITE_HOSTNAMES:=${SITE_DOMAIN:-} www.${SITE_DOMAIN:-}}"
  : "${SITE_ROOT:=.}"
  : "${SITE_JSON:=site.json}"
  : "${TARGET_PORT:=8080}"
  : "${DOCKERFILE:=Dockerfile.azure}"
  : "${NODE_IMAGE:=node:22-alpine}"
  : "${SERVER_ENTRY:=dist/src/index.js}"
  : "${BUILD_SCRIPT:=build}"
  : "${HEALTH_PATH:=/health}"
  : "${DOCKER_EXTRA_PATHS=}"
  : "${DOCKER_PLATFORM:=linux/amd64}"
  : "${CPU:=0.5}"
  : "${MEMORY:=1Gi}"
  : "${MIN_REPLICAS:=1}"
  : "${MAX_REPLICAS:=3}"
  : "${SECRETS_FILE:=~/.reusable-agents/secrets.env}"
  : "${APP_ENV=NODE_ENV=production}"
  : "${APP_SECRETS=}"
  : "${APP_SECRET_ENV=}"
  : "${AI_TRAFFIC_EXCLUDE_IPS=}"
  : "${DB_PORT:=5432}"
  : "${DB_SSLMODE:=require}"
  : "${DB_OWNER_CONNECTION_LIMIT:=3}"
  : "${DB_APP_CONNECTION_LIMIT:=20}"
  : "${DB_AGENTS_CONNECTION_LIMIT:=10}"
  : "${DB_PUBLISHER_CONNECTION_LIMIT:=2}"
  : "${DB_OWNER_STATEMENT_TIMEOUT:=15min}"
  : "${DB_APP_STATEMENT_TIMEOUT:=15s}"
  : "${DB_AGENTS_STATEMENT_TIMEOUT:=5min}"
  : "${DB_PUBLISHER_STATEMENT_TIMEOUT:=30s}"
  : "${DB_IDLE_IN_TRANSACTION_TIMEOUT:=60s}"
  : "${DB_APP_GRANTS:=SELECT:* INSERT:outbound_clicks,ai_traffic_log}"
  : "${DB_AGENTS_GRANTS:=SELECT,INSERT,UPDATE,DELETE:*}"
  : "${DB_PUBLISHER_GRANTS:=SELECT,UPDATE(status,published_at):editorial_articles}"
  : "${DB_OWNER_DSN=}" "${DB_APP_DSN=}" "${DB_AGENTS_DSN=}" "${DB_PUBLISHER_DSN=}"
  : "${PREDEPLOY_GATE_SUITE:=tests/run-local-suite.sh}"
  : "${PREDEPLOY_GATE_ARGS=--workers=4}"
  : "${MIGRATE_COMMAND=}"
  : "${SMOKE_PATHS:=/ /health /robots.txt /llms.txt /sitemap.xml}"
  : "${SMOKE_TIMEOUT_SECONDS:=180}"
  : "${HTML_MAX_S_MAXAGE:=600}"
  : "${READY_TIMEOUT_SECONDS:=600}"
  : "${AZ_SUBSCRIPTION=}" "${AZ_RESOURCE_GROUP=}" "${AZ_LOCATION=}" "${AZ_ENVIRONMENT=}" "${AZ_ACR=}"
  : "${AZ_LOG_WORKSPACE=}" "${AZ_PG_SERVER=}" "${AZ_DB_HOST=}" "${AZ_DB_ADMIN_USER=}" "${AZ_DB_ADMIN_PASSWORD=}"
  : "${AWS_STATE_FILE:=~/.aws-deploy/state.env}"
  # No fleet names here: the secret id and the admin login come from deploy.env
  # (the AWS scripts require them, as the Azure ones require AZ_*).
  : "${AWS_SECRET_ID=}"
  : "${AWS_TARGET_GROUP:=tg-${APP_NAME:-}}"
  : "${AWS_CPU_TARGET_PERCENT:=60}"
  : "${AWS_SSL_POLICY:=ELBSecurityPolicy-TLS13-1-2-2021-06}"
  : "${AWS_DB_HOST=}"
  : "${AWS_DB_ADMIN_USER=}"
  : "${AWS_DB_ADMIN_PASSWORD=}"
  : "${AWS_APP_SECRETS:=$APP_SECRETS}"
  : "${AWS_DB_OWNER_DSN:=$DB_OWNER_DSN}" "${AWS_DB_APP_DSN:=$DB_APP_DSN}"
  : "${AWS_DB_AGENTS_DSN:=$DB_AGENTS_DSN}" "${AWS_DB_PUBLISHER_DSN:=$DB_PUBLISHER_DSN}"

  SITE_ROOT=$(kd_expand_home "$SITE_ROOT")
  [[ $SITE_ROOT == /* ]] || SITE_ROOT="$KD_ENV_DIR/$SITE_ROOT"
  if [[ -d $SITE_ROOT ]]; then SITE_ROOT=$(cd "$SITE_ROOT" && pwd -P); fi
  KD_SECRETS_FILE=$(kd_expand_home "$SECRETS_FILE")
  [[ $KD_SECRETS_FILE == /* ]] || KD_SECRETS_FILE="$KD_ENV_DIR/$KD_SECRETS_FILE"
  AWS_STATE_FILE=$(kd_expand_home "$AWS_STATE_FILE")
  [[ $AWS_STATE_FILE == /* ]] || AWS_STATE_FILE="$KD_ENV_DIR/$AWS_STATE_FILE"
  read -ra KD_SMOKE_PATHS <<< "$SMOKE_PATHS"
  KD_SMOKE_UA="affiliate-kit-deploy-smoke/${KD_KIT_VERSION} (+https://${SITE_DOMAIN:-})"
}

kd_require_keys() {
  local k missing=()
  for k in "$@"; do [[ -n ${!k-} ]] || missing+=("$k"); done
  ((${#missing[@]} == 0)) || kd_die "$KD_ENV_FILE: set ${missing[*]}" 2
}

kd_check_re() {  # KEY REGEX HINT
  [[ ${!1-} =~ $2 ]] || kd_die "$KD_ENV_FILE: $1='${!1-}' is invalid: $3" 2
}

kd_is_int() { [[ $1 =~ ^[0-9]+$ ]]; }

kd_check_int() {  # KEY MIN MAX
  local v=${!1-}
  if ! kd_is_int "$v" || ((10#$v < $2 || 10#$v > $3)); then
    kd_die "$KD_ENV_FILE: $1='$v' must be an integer from $2 to $3" 2
  fi
}

# CPU in quarter vCPUs (0.25 steps); MEMORY in half GiB. Container Apps (and the
# Fargate sizes we map to) need memory = 2 x CPU.
kd_cpu_quarters() {
  [[ $1 =~ ^([0-9]+)(\.(0|00|25|5|50|75))?$ ]] || return 1
  local whole=${BASH_REMATCH[1]} frac=${BASH_REMATCH[3]:-0} q
  case $frac in 0|00) q=0 ;; 25) q=1 ;; 5|50) q=2 ;; 75) q=3 ;; esac
  printf '%s' $((10#$whole * 4 + q))
}

kd_memory_halves() {
  [[ $1 =~ ^([0-9]+)(\.(0|5))?Gi$ ]] || return 1
  local whole=${BASH_REMATCH[1]} frac=${BASH_REMATCH[3]:-0}
  printf '%s' $((10#$whole * 2 + (frac == 5 ? 1 : 0)))
}

kd_validate_common() {
  local h e name ref seen=" " envs=" " secrets=" " pool tok kind role roles=" " v
  kd_require_keys APP_NAME SITE_DOMAIN DB_NAME DB_OWNER_ROLE DB_APP_ROLE DB_AGENTS_ROLE DB_PUBLISHER_ROLE
  kd_check_re APP_NAME '^[a-z][a-z0-9-]{0,27}[a-z0-9]$' 'lowercase letters, digits and -, 2-29 chars, starting with a letter'
  [[ $APP_NAME != *--* ]] || kd_die "$KD_ENV_FILE: APP_NAME must not contain '--'" 2
  kd_check_re SITE_DOMAIN '^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$' 'a lowercase domain name'
  local -a hosts=()
  read -ra hosts <<< "$SITE_HOSTNAMES"
  ((${#hosts[@]} > 0)) || kd_die "$KD_ENV_FILE: SITE_HOSTNAMES is empty" 2
  for h in "${hosts[@]}"; do
    [[ $h == "$SITE_DOMAIN" || $h == *".$SITE_DOMAIN" ]] \
      || kd_die "$KD_ENV_FILE: SITE_HOSTNAMES entry '$h' is not $SITE_DOMAIN or a subdomain of it" 2
    [[ $h =~ ^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$ ]] || kd_die "$KD_ENV_FILE: bad hostname '$h'" 2
    [[ $seen != *" $h "* ]] || kd_die "$KD_ENV_FILE: SITE_HOSTNAMES lists '$h' twice" 2
    seen+="$h "
  done
  kd_check_int TARGET_PORT 1 65535
  kd_check_re DOCKER_PLATFORM '^linux/(amd64|arm64)$' 'linux/amd64 or linux/arm64'

  local cq mh
  cq=$(kd_cpu_quarters "$CPU") || kd_die "$KD_ENV_FILE: CPU='$CPU' must be vCPUs in 0.25 steps (e.g. 0.5)" 2
  mh=$(kd_memory_halves "$MEMORY") || kd_die "$KD_ENV_FILE: MEMORY='$MEMORY' must be GiB in 0.5 steps (e.g. 1Gi)" 2
  ((cq >= 1 && cq <= 16)) || kd_die "$KD_ENV_FILE: CPU must be between 0.25 and 4" 2
  ((mh == cq)) || kd_die "$KD_ENV_FILE: MEMORY must be twice CPU (CPU=$CPU needs $((cq / 2)).$(((cq % 2) * 5))Gi)" 2
  # Fargate units, read by aws/*.sh.
  # shellcheck disable=SC2034
  KD_CPU_UNITS=$((cq * 256))
  # shellcheck disable=SC2034
  KD_MEMORY_MIB=$((mh * 512))
  kd_is_int "$MIN_REPLICAS" || kd_die "$KD_ENV_FILE: MIN_REPLICAS must be an integer" 2
  ((10#$MIN_REPLICAS >= 1)) || kd_die "$KD_ENV_FILE: MIN_REPLICAS=$MIN_REPLICAS: scale-to-zero is not allowed (cold starts time out AI-assistant fetches; managed-certificate renewal needs a running app). Use at least 1." 2
  kd_check_int MIN_REPLICAS 1 30
  kd_check_int MAX_REPLICAS "$MIN_REPLICAS" 30

  # Plain env: no secrets, ever.
  local -a ev=() se=() as=()
  read -ra ev <<< "$APP_ENV"
  for e in "${ev[@]}"; do
    [[ $e =~ ^([A-Z_][A-Z0-9_]*)=([^[:space:]]*)$ ]] || kd_die "$KD_ENV_FILE: APP_ENV entry '$e' must be NAME=value" 2
    name=${BASH_REMATCH[1]}
    v=${BASH_REMATCH[2]}
    [[ $name != PORT ]] || kd_die "$KD_ENV_FILE: APP_ENV must not set PORT (it comes from TARGET_PORT)" 2
    [[ $name != AI_TRAFFIC_EXCLUDE_IPS ]] || kd_die "$KD_ENV_FILE: set AI_TRAFFIC_EXCLUDE_IPS as its own key, not in APP_ENV" 2
    if [[ ${name^^} =~ (PASSWORD|PASSWD|SECRET|TOKEN|DATABASE_URL|DSN|API_KEY|APIKEY|CONNECTION_STRING|PRIVATE_KEY|CREDENTIAL) ]]; then
      kd_die "$KD_ENV_FILE: APP_ENV sets $name, which looks like a secret: put it in APP_SECRETS / APP_SECRET_ENV" 2
    fi
    [[ ! $v =~ ://[^/@]+:[^/@]+@ ]] || kd_die "$KD_ENV_FILE: APP_ENV $name carries credentials in a URL: use APP_SECRETS" 2
    [[ $envs != *" $name "* ]] || kd_die "$KD_ENV_FILE: APP_ENV sets $name twice" 2
    envs+="$name "
  done
  kd_validate_secret_list APP_SECRETS "$APP_SECRETS"
  kd_validate_secret_list AWS_APP_SECRETS "$AWS_APP_SECRETS"
  read -ra as <<< "$APP_SECRETS"
  for e in "${as[@]}"; do secrets+="${e%%=*} "; done
  read -ra se <<< "$APP_SECRET_ENV"
  for e in "${se[@]}"; do
    [[ $e =~ ^([A-Z_][A-Z0-9_]*)=([a-z0-9]([a-z0-9.-]*[a-z0-9])?)$ ]] \
      || kd_die "$KD_ENV_FILE: APP_SECRET_ENV entry '$e' must be ENV_NAME=secret-name" 2
    name=${BASH_REMATCH[1]}
    [[ $secrets == *" ${BASH_REMATCH[2]} "* ]] || kd_die "$KD_ENV_FILE: APP_SECRET_ENV $name reads secret '${BASH_REMATCH[2]}', which APP_SECRETS does not define" 2
    [[ $name != PORT && $envs != *" $name "* ]] || kd_die "$KD_ENV_FILE: $name is set both in APP_ENV and APP_SECRET_ENV" 2
    envs+="$name "
  done
  [[ -z $APP_SECRETS ]] && kd_warn "APP_SECRETS is empty: the container gets no secrets (no DATABASE_URL?)"
  [[ -z $AI_TRAFFIC_EXCLUDE_IPS || ${AI_TRAFFIC_EXCLUDE_IPS// /} =~ ^[0-9A-Fa-f.:/,]+$ ]] \
    || kd_die "$KD_ENV_FILE: AI_TRAFFIC_EXCLUDE_IPS must be comma-separated IPs/CIDRs" 2

  # Database.
  kd_check_re DB_NAME '^[a-z_][a-z0-9_]{0,62}$' 'a lowercase SQL identifier'
  case $DB_NAME in postgres|template0|template1|azure_maintenance|azure_sys|rdsadmin)
    kd_die "$KD_ENV_FILE: DB_NAME '$DB_NAME' is reserved" 2 ;;
  esac
  kd_check_int DB_PORT 1 65535
  kd_check_re DB_SSLMODE '^(disable|allow|prefer|require|verify-ca|verify-full)$' 'a libpq sslmode'
  for kind in "${KD_ROLE_KINDS[@]}"; do
    kd_check_re "DB_${kind}_ROLE" '^[a-z_][a-z0-9_]{0,62}$' 'a lowercase SQL identifier'
    role=$(kd_var "DB_${kind}_ROLE")
    [[ $role != pg_* && $role != azure* && $role != postgres && $role != rds* ]] \
      || kd_die "$KD_ENV_FILE: DB_${kind}_ROLE '$role' is a reserved name" 2
    [[ $roles != *" $role "* ]] || kd_die "$KD_ENV_FILE: the four DB roles must be distinct ($role repeats)" 2
    roles+="$role "
    kd_check_int "DB_${kind}_CONNECTION_LIMIT" 1 500
    kd_check_re "DB_${kind}_STATEMENT_TIMEOUT" '^[1-9][0-9]*(ms|s|min|h)?$' 'a positive Postgres duration (15s, 5min)'
    for ref in "$(kd_var "DB_${kind}_DSN")" "$(kd_var "AWS_DB_${kind}_DSN")"; do
      [[ -z $ref || $ref =~ ^(env:[A-Za-z_][A-Za-z0-9_]*|file:.+)$ ]] \
        || kd_die "$KD_ENV_FILE: DB_${kind}_DSN must be env:NAME or file:PATH" 2
    done
  done
  [[ $DB_IDLE_IN_TRANSACTION_TIMEOUT == off ]] \
    || kd_check_re DB_IDLE_IN_TRANSACTION_TIMEOUT '^[1-9][0-9]*(ms|s|min|h)?$' "a positive Postgres duration or 'off'"
  local privs
  for kind in APP AGENTS PUBLISHER; do
    local -a toks=()
    read -ra toks <<< "$(kd_var "DB_${kind}_GRANTS")"
    for tok in "${toks[@]}"; do
      if [[ $tok != *:* ]] || ! kd_grant_privs "${tok%%:*}" privs; then
        kd_die "$KD_ENV_FILE: DB_${kind}_GRANTS entry '$tok' must be PRIVILEGES:TABLES with PRIVILEGES from SELECT, INSERT, UPDATE, DELETE, each optionally limited to columns (e.g. SELECT,UPDATE(status,published_at):editorial_articles or SELECT:*)" 2
      fi
      [[ ${tok#*:} =~ ^(\*|[a-z_][a-z0-9_]*(,[a-z_][a-z0-9_]*)*)$ ]] \
        || kd_die "$KD_ENV_FILE: DB_${kind}_GRANTS entry '$tok': TABLES must be * or lowercase table names separated by commas" 2
      [[ ${tok#*:} != '*' || $privs != *'('* ]] \
        || kd_die "$KD_ENV_FILE: DB_${kind}_GRANTS entry '$tok': column privileges need named tables, not *" 2
    done
  done
  for ref in "$AZ_DB_ADMIN_PASSWORD" "$AWS_DB_ADMIN_PASSWORD"; do
    [[ -z $ref || $ref =~ ^(env:[A-Za-z_][A-Za-z0-9_]*|file:.+)$ ]] \
      || kd_die "$KD_ENV_FILE: AZ_DB_ADMIN_PASSWORD / AWS_DB_ADMIN_PASSWORD must be env:NAME or file:PATH" 2
  done
  # The app's pool, multiplied by its replicas, must fit its connection limit.
  pool=""
  for e in "${ev[@]}"; do [[ $e == PG_POOL_MAX=* ]] && pool=${e#PG_POOL_MAX=}; done
  if [[ -n $pool ]]; then
    kd_is_int "$pool" || kd_die "$KD_ENV_FILE: PG_POOL_MAX must be an integer" 2
    ((10#$pool * 10#$MAX_REPLICAS <= 10#$DB_APP_CONNECTION_LIMIT)) \
      || kd_die "$KD_ENV_FILE: PG_POOL_MAX=$pool x MAX_REPLICAS=$MAX_REPLICAS exceeds DB_APP_CONNECTION_LIMIT=$DB_APP_CONNECTION_LIMIT" 2
  else
    kd_warn "APP_ENV has no PG_POOL_MAX: make sure the app's pool size x MAX_REPLICAS ($MAX_REPLICAS) stays within DB_APP_CONNECTION_LIMIT ($DB_APP_CONNECTION_LIMIT)"
  fi

  # Release and smoke.
  kd_check_int HTML_MAX_S_MAXAGE 0 600
  kd_check_int SMOKE_TIMEOUT_SECONDS 5 3600
  kd_check_int READY_TIMEOUT_SECONDS 30 3600
  ((${#KD_SMOKE_PATHS[@]} > 0)) || kd_die "$KD_ENV_FILE: SMOKE_PATHS is empty" 2
  for e in "${KD_SMOKE_PATHS[@]}"; do
    [[ $e =~ ^/[^[:space:]=]*(=[1-5][0-9][0-9])?$ ]] || kd_die "$KD_ENV_FILE: SMOKE_PATHS entry '$e' must be /path or /path=<status>" 2
  done
  local shell_re='[;&|<>`$]'
  [[ ! $MIGRATE_COMMAND =~ $shell_re ]] \
    || kd_die "$KD_ENV_FILE: MIGRATE_COMMAND is run without a shell: plain words only" 2
}

# kd_grant_privs PRIVS OUTVAR: a DB_*_GRANTS privilege list as SQL, e.g.
# SELECT,UPDATE(status,published_at) -> SELECT, UPDATE ("status", "published_at").
# Column lists are allowed on SELECT, INSERT and UPDATE. Returns 1 when invalid.
kd_grant_privs() {
  local _gp_in=$1 _gp_dst=$2 _gp_out="" _gp_p _gp_cols
  local _gp_re='^([A-Za-z]+)(\(([a-z_][a-z0-9_]*(,[a-z_][a-z0-9_]*)*)\))?(,|$)'
  [[ -n $_gp_in ]] || return 1
  while [[ -n $_gp_in ]]; do
    [[ $_gp_in =~ $_gp_re ]] || return 1
    _gp_p=${BASH_REMATCH[1]^^}
    _gp_cols=${BASH_REMATCH[3]}
    case $_gp_p in SELECT|INSERT|UPDATE|DELETE) ;; *) return 1 ;; esac
    _gp_in=${_gp_in:${#BASH_REMATCH[0]}}
    # "SELECT," (a trailing comma) is not a list
    [[ ${BASH_REMATCH[5]} != , || -n $_gp_in ]] || return 1
    if [[ -n $_gp_cols ]]; then
      [[ $_gp_p != DELETE ]] || return 1
      _gp_out+="${_gp_out:+, }$_gp_p (\"${_gp_cols//,/\", \"}\")"
    else
      _gp_out+="${_gp_out:+, }$_gp_p"
    fi
  done
  printf -v "$_gp_dst" '%s' "$_gp_out"
}

kd_validate_secret_list() {  # KEY VALUE
  local key=$1 list=$2 e names=" "
  local -a items=()
  read -ra items <<< "$list"
  for e in "${items[@]}"; do
    [[ $e =~ ^([a-z0-9]([a-z0-9.-]*[a-z0-9])?)=(env:[A-Za-z_][A-Za-z0-9_]*|file:[^[:space:]]+)$ ]] \
      || kd_die "$KD_ENV_FILE: $key entry '$e' must be secret-name=env:NAME or secret-name=file:PATH" 2
    [[ $names != *" ${BASH_REMATCH[1]} "* ]] || kd_die "$KD_ENV_FILE: $key defines '${BASH_REMATCH[1]}' twice" 2
    names+="${BASH_REMATCH[1]} "
  done
}

# KD_CENV: plain container env (NAME=value); KD_CSECRET_ENV: NAME=secret-name.
kd_container_env() {
  local -a ev=()
  KD_CENV=("PORT=$TARGET_PORT")
  read -ra ev <<< "$APP_ENV"
  KD_CENV+=("${ev[@]}")
  [[ -n $KD_AI_EXCLUDE ]] && KD_CENV+=("AI_TRAFFIC_EXCLUDE_IPS=$KD_AI_EXCLUDE")
  # shellcheck disable=SC2034  # read by azure.sh / aws.sh
  read -ra KD_CSECRET_ENV <<< "$APP_SECRET_ENV"
  return 0
}

# AI_TRAFFIC_EXCLUDE_IPS: deploy.env, else the environment, else SECRETS_FILE,
# else this host's public egress address (deploys run from the fleet host).
kd_resolve_ai_exclude() {
  local v=${AI_TRAFFIC_EXCLUDE_IPS:-}
  [[ -n $v ]] || v=$KD_ENV_AI_TRAFFIC_EXCLUDE_IPS
  [[ -n $v ]] || kd_secrets_file_lookup AI_TRAFFIC_EXCLUDE_IPS v || v=""
  if [[ -z $v ]]; then
    kd_probe_value v egress-ip curl -fsS -m 5 https://api.ipify.org
    if ((KD_DRY_RUN)); then
      KD_AI_EXCLUDE=$v
      return 0
    fi
    [[ $v =~ ^[0-9a-fA-F.:]+$ ]] || v=""
  fi
  v=${v// /}
  [[ -z $v || $v =~ ^[0-9A-Fa-f.:/,]+$ ]] || kd_die "AI_TRAFFIC_EXCLUDE_IPS must be comma-separated IPs/CIDRs" 2
  [[ -n $v ]] || kd_warn "AI_TRAFFIC_EXCLUDE_IPS is empty: this host's audit and smoke requests will count as AI traffic"
  KD_AI_EXCLUDE=$v
}

# ─── database (both clouds) ───────────────────────────────────────────────────

kd_role_name() { kd_var "DB_${1}_ROLE"; }

# KD_PSQL: psql from PATH, else psql inside a postgres image (KIT_DEPLOY_PSQL overrides).
kd_psql_cmd() {
  if [[ -n ${KIT_DEPLOY_PSQL:-} ]]; then
    read -ra KD_PSQL <<< "$KIT_DEPLOY_PSQL"
  elif command -v psql >/dev/null 2>&1; then
    KD_PSQL=(psql)
  else
    KD_PSQL=(docker run --rm -i --network host -e PGPASSWORD -e PGCONNECT_TIMEOUT
      "${KIT_DEPLOY_PSQL_IMAGE:-postgres:16-alpine}" psql)
  fi
}

# kd_psql_exec HOST USER DBNAME PASSWORD_REF LABEL <<< SQL
kd_psql_exec() {
  local host=$1 user=$2 db=$3 pwref=$4 label=$5 sql pw=""
  sql=$(cat)
  local conninfo="host=$host port=$DB_PORT dbname=$db user=$user sslmode=$DB_SSLMODE application_name=affiliate-kit-deploy"
  local -a KD_PSQL=()
  kd_psql_cmd
  local -a cmd=("${KD_PSQL[@]}" "$conninfo" -X -q -v ON_ERROR_STOP=1 -f -)
  if ((KD_DRY_RUN)); then
    kd_guard_args "${cmd[@]}"
    [[ $sql != *'SCRAM-SHA-256$'* ]] || kd_die "internal error: a password verifier reached dry-run output" 70
    printf "[dry-run] PGPASSWORD=<%s> %s <<'SQL'\n%s\nSQL\n" "$pwref" "$(kd_qcmd "${cmd[@]}")" "$sql"
    return 0
  fi
  [[ ${KD_PSQL[0]} == docker ]] && kd_need_tool docker
  kd_secret_get "$pwref" pw || kd_die "the database admin password ($pwref) is not set" 3
  kd_guard_args "${cmd[@]}"
  kd_log "psql: $label ($user@$host:$DB_PORT/$db)"
  (
    export PGPASSWORD="$pw" PGCONNECT_TIMEOUT=15
    exec "${cmd[@]}"
  ) <<< "$sql"
}

# kd_role_verifier KIND REF HOST: KD_ROLE_PASSWORD[KIND] = the quoted SCRAM
# verifier of the password in the role's DSN. Returns 1 when REF is unset.
kd_role_verifier() {
  local kind=$1 ref=$2 host=$3 dsn="" v role
  local -a expect=()
  role=$(kd_role_name "$kind")
  [[ -n $ref ]] || return 1
  kd_secret_get "$ref" dsn || return 1
  # A dry-run host placeholder (<db-host>) is not a host to compare with.
  [[ $host == '<'* ]] || expect=(--expect-host "$host")
  v=$("$KD_PYTHON" -I "$KD_TOOL" role-verifier --dsn-file - --expect-user "$role" --expect-db "$DB_NAME" \
        "${expect[@]}" --expect-port "$DB_PORT" --label "DB_${kind}_DSN ($ref)" <<< "$dsn") \
    || kd_die "DB_${kind}_DSN ($ref) does not fit role $role / database $DB_NAME" 3
  if ((KD_DRY_RUN)); then
    KD_ROLE_PASSWORD[$kind]="'<scram-sha-256 verifier of the password in $ref>'"
  else
    kd_register_secret "$v"
    KD_ROLE_PASSWORD[$kind]="'$v'"
  fi
}

# kd_role_new_dsn KIND HOST FILE VAR: generate a password, append VAR='dsn' to FILE.
kd_role_new_dsn() {
  local kind=$1 host=$2 file=$3 var=$4 role v
  role=$(kd_role_name "$kind")
  if ((KD_DRY_RUN)); then
    printf "[dry-run] generate a password for %s; append %s='postgresql://%s:<generated>@%s:%s/%s?sslmode=%s' to %s (0600)\n" \
      "$role" "$var" "$role" "$host" "$DB_PORT" "$DB_NAME" "$DB_SSLMODE" "$file"
    KD_ROLE_PASSWORD[$kind]="'<scram-sha-256 verifier of a generated password>'"
    return 0
  fi
  v=$("$KD_PYTHON" -I "$KD_TOOL" new-dsn --user "$role" --host "$host" --port "$DB_PORT" --db "$DB_NAME" \
        --sslmode "$DB_SSLMODE" --var "$var" --out "$file") || kd_die "could not generate a DSN for $role" 1
  kd_register_secret "$v"
  KD_ROLE_PASSWORD[$kind]="'$v'"
  kd_log "wrote $var for role $role to $file (merge it into $KD_SECRETS_FILE, single-quoted as written)"
}

kd_sql_role_list() {  # quoted, comma-separated role names for KINDS...
  local out="" k
  for k in "$@"; do out+="${out:+, }\"$(kd_role_name "$k")\""; done
  printf '%s' "$out"
}

kd_sql_role_literals() {  # the four role names as SQL string literals
  local out="" k
  for k in "${KD_ROLE_KINDS[@]}"; do out+="${out:+, }'$(kd_role_name "$k")'"; done
  printf '%s' "$out"
}

kd_marker() { printf '%s%s' "$KD_MARKER_PREFIX" "$APP_NAME"; }

# kd_sql_owned_check ADOPT ACTION HINT: a DO block that raises before anything
# changes when one of the four roles or the database exists but is not this
# site's. The kit marks what it creates with COMMENT '<KD_MARKER_PREFIX><APP_NAME>':
# a role in the transaction that creates it, the database right after CREATE
# DATABASE (which cannot run in a transaction), so an unmarked database owned
# by this site's marked owner role (a first run stopped in between) counts as
# this site's. ADOPT=1 (provision --adopt-existing) also accepts objects with
# no kit marker; another kit site's objects never.
kd_sql_owned_check() {
  local adopt=false action=$2 hint=$3 dq="\$kit\$"
  if (($1)); then adopt=true; fi
  cat <<SQL
DO ${dq}
DECLARE
  marker constant text := '$(kd_marker)';
  adopt constant boolean := ${adopt};
  owner_ours boolean;
  strangers text;
BEGIN
  SELECT coalesce(shobj_description(oid, 'pg_authid') = marker, false) INTO owner_ours
    FROM pg_catalog.pg_roles WHERE rolname = '${DB_OWNER_ROLE}';
  SELECT string_agg(what, '; ' ORDER BY what) INTO strangers FROM (
    SELECT format('role %s (%s)', r.rolname, coalesce(c.descr, 'no affiliate-kit marker')) AS what
      FROM pg_catalog.pg_roles r
      CROSS JOIN LATERAL (SELECT shobj_description(r.oid, 'pg_authid') AS descr) c
     WHERE r.rolname IN ($(kd_sql_role_literals))
       AND c.descr IS DISTINCT FROM marker
       AND NOT (adopt AND coalesce(c.descr, '') NOT LIKE '${KD_MARKER_PREFIX}%')
    UNION ALL
    SELECT format('database %s (owner %s, %s)', d.datname, pg_catalog.pg_get_userbyid(d.datdba),
                  coalesce(c.descr, 'no affiliate-kit marker'))
      FROM pg_catalog.pg_database d
      CROSS JOIN LATERAL (SELECT shobj_description(d.oid, 'pg_database') AS descr) c
     WHERE d.datname = '${DB_NAME}'
       AND c.descr IS DISTINCT FROM marker
       AND NOT (c.descr IS NULL AND coalesce(owner_ours, false)
                AND pg_catalog.pg_get_userbyid(d.datdba) = '${DB_OWNER_ROLE}')
       AND NOT (adopt AND coalesce(c.descr, '') NOT LIKE '${KD_MARKER_PREFIX}%')
  ) objs;
  IF strangers IS NOT NULL THEN
    RAISE EXCEPTION 'affiliate-kit (%, app ${APP_NAME}): not created by the kit for this site, so nothing was changed: %', '${action}', strangers
      USING HINT = '${hint}';
  END IF;
END
${dq};
SQL
}

kd_sql_cluster() {
  local kind role limit timeout idle=$DB_IDLE_IN_TRANSACTION_TIMEOUT dq="\$kit\$" names marker
  names=$(kd_sql_role_literals)
  marker=$(kd_marker)
  printf -- '-- affiliate-kit: cluster objects for database "%s" (roles, database, CONNECT)\n' "$DB_NAME"
  printf 'SET client_min_messages = warning;\n\n'
  printf -- '-- before anything changes: every one of these that exists must be this site%ss\n' "'"
  kd_sql_owned_check "$KD_ADOPT" provision "Check DB_NAME and DB_*_ROLE in deploy.env: another site or service uses these names. To take over objects that predate the kit, re-run provision.sh database with --adopt-existing."
  printf '\n-- the four roles, each marked as this site%ss in the transaction that creates it\nBEGIN;\n' "'"
  for kind in "${KD_ROLE_KINDS[@]}"; do
    role=$(kd_role_name "$kind")
    limit=$(kd_var "DB_${kind}_CONNECTION_LIMIT")
    timeout=$(kd_var "DB_${kind}_STATEMENT_TIMEOUT")
    printf '\n-- %s role %s\n' "${kind,,}" "$role"
    printf "SELECT 'CREATE ROLE \"%s\" WITH LOGIN NOCREATEDB NOCREATEROLE CONNECTION LIMIT %s'\n" "$role" "$limit"
    printf " WHERE NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = '%s') \\\\gexec\n" "$role"
    printf "COMMENT ON ROLE \"%s\" IS '%s';\n" "$role" "$marker"
    printf 'ALTER ROLE "%s" WITH LOGIN NOCREATEDB NOCREATEROLE CONNECTION LIMIT %s;\n' "$role" "$limit"
    if [[ -n ${KD_ROLE_PASSWORD[$kind]-} ]]; then
      printf 'ALTER ROLE "%s" WITH PASSWORD %s;\n' "$role" "${KD_ROLE_PASSWORD[$kind]}"
    fi
    printf "ALTER ROLE \"%s\" SET statement_timeout = '%s';\n" "$role" "$timeout"
    if [[ $idle == off ]]; then
      printf 'ALTER ROLE "%s" RESET idle_in_transaction_session_timeout;\n' "$role"
    else
      printf "ALTER ROLE \"%s\" SET idle_in_transaction_session_timeout = '%s';\n" "$role" "$idle"
    fi
  done
  cat <<SQL

-- the database belongs to the owner role (the admin needs SET ROLE on it to hand it over)
GRANT "${DB_OWNER_ROLE}" TO CURRENT_USER;
COMMIT;

-- the database (CREATE DATABASE cannot run inside a transaction), then its marker
SELECT 'CREATE DATABASE "${DB_NAME}" WITH OWNER "${DB_OWNER_ROLE}" ENCODING ''UTF8'' TEMPLATE template0'
 WHERE NOT EXISTS (SELECT FROM pg_catalog.pg_database WHERE datname = '${DB_NAME}') \gexec
COMMENT ON DATABASE "${DB_NAME}" IS '${marker}';
ALTER DATABASE "${DB_NAME}" OWNER TO "${DB_OWNER_ROLE}";
REVOKE ALL ON DATABASE "${DB_NAME}" FROM PUBLIC;
GRANT CONNECT ON DATABASE "${DB_NAME}" TO $(kd_sql_role_list APP AGENTS PUBLISHER);

-- least privilege, a CONNECTION LIMIT, a statement_timeout and this site's marker on every role, or fail
DO ${dq}
DECLARE bad text;
BEGIN
  SELECT string_agg(rolname, ', ' ORDER BY rolname) INTO bad FROM pg_catalog.pg_roles
   WHERE rolname IN (${names})
     AND (rolsuper OR rolcreaterole OR rolcreatedb OR rolreplication OR rolbypassrls
          OR NOT rolcanlogin OR rolconnlimit < 0);
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'affiliate-kit: roles with more than least privilege or no CONNECTION LIMIT: %', bad;
  END IF;
  SELECT string_agg(r.rolname, ', ' ORDER BY r.rolname) INTO bad FROM pg_catalog.pg_roles r
   WHERE r.rolname IN (${names})
     AND NOT EXISTS (SELECT FROM pg_catalog.pg_db_role_setting s
                      WHERE s.setrole = r.oid AND s.setdatabase = 0
                        AND EXISTS (SELECT FROM unnest(s.setconfig) c WHERE c LIKE 'statement_timeout=%'));
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'affiliate-kit: roles without a statement_timeout: %', bad;
  END IF;
  SELECT string_agg(rolname, ', ' ORDER BY rolname) INTO bad FROM pg_catalog.pg_roles
   WHERE rolname IN (${names}) AND shobj_description(oid, 'pg_authid') IS DISTINCT FROM '${marker}';
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'affiliate-kit: roles without the marker %: %', '${marker}', bad;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_catalog.pg_database
                  WHERE datname = '${DB_NAME}' AND shobj_description(oid, 'pg_database') = '${marker}') THEN
    RAISE EXCEPTION 'affiliate-kit: database ${DB_NAME} lacks the marker %', '${marker}';
  END IF;
END
${dq};
SQL
}

kd_sql_table_grant() {  # ROLE PRIVS TABLE
  local role=$1 privs=$2 t=$3 dq="\$kit\$" seqs=""
  if [[ $privs == *INSERT* ]]; then
    seqs="
    FOR seq IN
      SELECT d.objid::regclass FROM pg_catalog.pg_depend d
        JOIN pg_catalog.pg_class c ON c.oid = d.objid
       WHERE d.classid = 'pg_catalog.pg_class'::regclass AND d.refclassid = 'pg_catalog.pg_class'::regclass
         AND d.refobjid = 'public.${t}'::regclass AND c.relkind = 'S' AND d.deptype IN ('a', 'i')
    LOOP
      EXECUTE format('GRANT USAGE, SELECT ON SEQUENCE %s TO %I', seq, '${role}');
    END LOOP;"
  fi
  cat <<SQL
DO ${dq}
DECLARE seq regclass;
BEGIN
  IF to_regclass('public.${t}') IS NULL THEN
    RAISE WARNING 'affiliate-kit: table public.${t} does not exist yet; re-run the database phase after the migrations create it';
  ELSE
    EXECUTE format('GRANT ${privs} ON public.%I TO %I', '${t}', '${role}');${seqs}
  END IF;
END
${dq};
SQL
}

kd_sql_grants() {  # KIND
  local kind=$1 role grants tok privs tables t
  local -a toks=() tl=()
  role=$(kd_role_name "$kind")
  grants=$(kd_var "DB_${kind}_GRANTS")
  printf '\n-- %s: %s\n' "$role" "$grants"
  read -ra toks <<< "$grants"
  for tok in "${toks[@]}"; do
    kd_grant_privs "${tok%%:*}" privs || return 1   # validated in kd_validate_common
    tables=${tok#*:}
    if [[ $tables == '*' ]]; then
      printf 'GRANT %s ON ALL TABLES IN SCHEMA public TO "%s";\n' "$privs" "$role"
      printf 'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT %s ON TABLES TO "%s";\n' "$privs" "$role"
      if [[ $privs == *INSERT* ]]; then
        printf 'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO "%s";\n' "$role"
        printf 'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO "%s";\n' "$role"
      fi
    else
      IFS=, read -ra tl <<< "$tables"
      for t in "${tl[@]}"; do kd_sql_table_grant "$role" "$privs" "$t"; done
    fi
  done
}

kd_sql_database() {
  local others dq="\$kit\$"
  others=$(kd_sql_role_list APP AGENTS PUBLISHER)
  cat <<SQL
-- affiliate-kit: privileges inside database "${DB_NAME}"; converges to DB_*_GRANTS
SET client_min_messages = warning;
BEGIN;
SET LOCAL ROLE "${DB_OWNER_ROLE}";
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO ${others};
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM ${others};
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM ${others};
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM ${others};
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM ${others};
SQL
  kd_sql_grants APP
  kd_sql_grants AGENTS
  kd_sql_grants PUBLISHER
  cat <<SQL

-- grants and default privileges only reach tables the owner role owns
DO ${dq}
DECLARE stray text;
BEGIN
  SELECT string_agg(tablename, ', ' ORDER BY tablename) INTO stray FROM pg_catalog.pg_tables
   WHERE schemaname = 'public' AND tableowner <> '${DB_OWNER_ROLE}';
  IF stray IS NOT NULL THEN
    RAISE WARNING 'affiliate-kit: tables not owned by ${DB_OWNER_ROLE} (run migrations as that role, or ALTER TABLE ... OWNER TO it): %', stray;
  END IF;
END
${dq};
COMMIT;
SQL
}

kd_sql_drop() {
  local kind
  printf -- '-- affiliate-kit: drop database "%s" and its four roles\n' "$DB_NAME"
  printf 'SET client_min_messages = warning;\n\n'
  printf -- '-- before anything is dropped: every one of these that exists must be this site%ss\n' "'"
  kd_sql_owned_check 0 teardown "Check DB_NAME and DB_*_ROLE in deploy.env: teardown drops only the database and roles the kit created for this APP_NAME."
  printf 'DROP DATABASE IF EXISTS "%s" WITH (FORCE);\n' "$DB_NAME"
  for kind in "${KD_ROLE_KINDS[@]}"; do
    printf 'DROP ROLE IF EXISTS "%s";\n' "$(kd_role_name "$kind")"
  done
}

# kd_db_provision HOST ADMIN_USER ADMIN_PASSWORD_REF NEW_DSN_FILE DSN_REF_PREFIX
# Roles (with passwords from their DSNs), the database, CONNECT, and the
# converging grants. DSN_REF_PREFIX is DB or AWS_DB (per-cloud DSN refs).
# Returns 3 when a role is left without a password.
kd_db_provision() {
  local host=$1 admin_user=$2 admin_ref=$3 newfile=$4 prefix=$5 kind ref role sql
  local -a nopass=()
  [[ -n $host ]] || kd_die "no database host" 2
  [[ -n $admin_user && -n $admin_ref ]] || kd_die "the database admin user and password reference must be set" 2
  if [[ -n $newfile ]]; then
    newfile=$(kd_expand_home "$newfile")
    [[ $newfile == /* ]] || newfile="$PWD/$newfile"
  fi
  KD_ROLE_PASSWORD=()
  for kind in "${KD_ROLE_KINDS[@]}"; do
    role=$(kd_role_name "$kind")
    ref=$(kd_var "${prefix}_${kind}_DSN")
    if kd_role_verifier "$kind" "$ref" "$host"; then
      continue
    fi
    if [[ -n $newfile ]]; then
      local var=${ref#env:}
      [[ $ref == env:* ]] || var="DATABASE_URL_$(tr '[:lower:]-' '[:upper:]_' <<< "$role")"
      kd_role_new_dsn "$kind" "$host" "$newfile" "$var"
    else
      nopass+=("$role")
    fi
  done
  sql=$(kd_sql_cluster)
  kd_psql_exec "$host" "$admin_user" postgres "$admin_ref" "roles, database and CONNECT" <<< "$sql" \
    || kd_die "the cluster-level SQL failed (the psql error is above; nothing after it ran)" 1
  sql=$(kd_sql_database)
  kd_psql_exec "$host" "$admin_user" "$DB_NAME" "$admin_ref" "schema privileges and grants" <<< "$sql" \
    || kd_die "the database-level SQL failed (rolled back)" 1
  if ((${#nopass[@]})); then
    kd_warn "roles without a password (their ${prefix}_*_DSN secret is not set): ${nopass[*]}"
    kd_warn "put each role's DSN in the environment or $KD_SECRETS_FILE and re-run, or re-run with --write-new-dsns FILE"
    return 3
  fi
  return 0
}

# ─── image, gate, migrations, smoke (both clouds) ─────────────────────────────

kd_default_tag() { date -u +%Y%m%d-%H%M%S; }

kd_check_tag() {
  [[ $1 =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]] || kd_die "bad image tag '$1'" 2
}

# kd_tag_from_ref VALUE REGISTRY/REPO: --image-tag also takes a full reference
# to this site's repository (a deployer rollback passes {prior_image}).
kd_tag_from_ref() {
  local v=$1 repo=$2
  if [[ $v == */* || $v == *:* ]]; then
    [[ $v == "$repo:"* ]] || kd_die "image '$v' is not in this site's repository $repo" 2
    v=${v#"$repo:"}
  fi
  kd_check_tag "$v"
  printf '%s' "$v"
}

# Offline checks before anything is built: the HTML cache limit in site.json,
# a rendered Dockerfile that matches the kit template, no secrets in the image.
kd_prebuild_checks() {
  local df="$SITE_ROOT/$DOCKERFILE" sj="$SITE_ROOT/$SITE_JSON" out
  [[ -d $SITE_ROOT ]] || kd_die "SITE_ROOT $SITE_ROOT does not exist" 2
  [[ -f $sj ]] || kd_die "site.json not found at $sj (SITE_JSON)" 2
  out=$("$KD_PYTHON" -I "$KD_TOOL" site-cache --site-json "$sj" --max "$HTML_MAX_S_MAXAGE") \
    || kd_die "site.json breaks the HTML cache limit (s-maxage <= $HTML_MAX_S_MAXAGE): fix cache.* first" 2
  kd_log "site.json cache: $out"
  [[ -f $df ]] || kd_die "Dockerfile not found: $df (render it: deploy/render-dockerfile.sh --env $KD_ENV_FILE --write)" 2
  local line content secret_re='^[[:space:]]*(ARG|ENV)[[:space:]].*(DATABASE_URL|PASSWORD|SECRET|TOKEN|API_KEY)'
  content=$(<"$df")
  while IFS= read -r line; do
    if [[ ${line^^} =~ $secret_re ]]; then
      kd_die "$df declares a secret-looking ARG/ENV: secrets reach the container as Container App / ECS secrets, never through the image" 2
    fi
  done <<< "$content"
  if [[ $content == *'# rendered-from: affiliate-kit/deploy/Dockerfile.azure.tmpl'* ]]; then
    kd_render_dockerfile --check "$df" || kd_die "$df has drifted from the kit template: re-render it (deploy/render-dockerfile.sh --write) and commit" 2
  else
    kd_warn "$df was not rendered from the kit template; it is built as is"
  fi
  [[ -f $SITE_ROOT/.dockerignore ]] || kd_warn "$SITE_ROOT has no .dockerignore (see deploy/dockerignore.example): the build context includes node_modules and .git"
  return 0
}

kd_render_dockerfile() {  # --out FILE | --check FILE
  "$KD_PYTHON" -I "$KD_TOOL" render-dockerfile --template "$KD_DEPLOY_DIR/Dockerfile.azure.tmpl" \
    --app-name "$APP_NAME" --kit-version "$KD_KIT_VERSION" --node-image "$NODE_IMAGE" --port "$TARGET_PORT" \
    --server-entry "$SERVER_ENTRY" --build-script "$BUILD_SCRIPT" --health-path "$HEALTH_PATH" \
    --extra-paths "$DOCKER_EXTRA_PATHS" "$@"
}

# ─── deploy.sh stage selection and the gate policy (both clouds) ──────────────

KD_ALL_STAGES=(gate build push migrate release smoke)
declare -A KD_STAGE=()

# kd_select_stages GIVEN LIST: KD_STAGE[stage]=1 for every stage to run: all of
# them unless --only (or --build-only, --skip-build, --smoke-only) gave a
# list, which must name at least one stage. An empty list is an error, never
# "everything": a template whose stage variable expands to nothing must not deploy.
kd_select_stages() {
  local given=$1 list=$2 s
  local -a wanted=()
  KD_STAGE=()
  if ((!given)); then
    for s in "${KD_ALL_STAGES[@]}"; do KD_STAGE[$s]=1; done
    return 0
  fi
  [[ -n ${list//[[:space:],]/} ]] || kd_die "--only needs at least one stage (stages: ${KD_ALL_STAGES[*]})" 2
  IFS=, read -ra wanted <<< "$list"
  for s in "${wanted[@]}"; do
    [[ " ${KD_ALL_STAGES[*]} " == *" $s "* ]] || kd_die "unknown stage '$s' (stages: ${KD_ALL_STAGES[*]})" 2
    KD_STAGE[$s]=1
  done
}

kd_has_stage() { [[ -n ${KD_STAGE[$1]-} ]]; }

# kd_gate_policy: untested code does not ship. A run that builds either runs the
# gate stage, or names who ran the gate on this tree (--gated-by, the deployer's
# own test stage), or skips it loudly (--skip-tests[=REASON]).
kd_gate_policy() {
  if [[ -n $KD_GATED_BY ]]; then
    [[ $KD_GATED_BY =~ ^[A-Za-z0-9][A-Za-z0-9._@:/-]{0,63}$ ]] \
      || kd_die "--gated-by needs a name (letters, digits and ._@:/-), e.g. --gated-by=deployer" 2
    ((!KD_SKIP_TESTS)) || kd_die "--gated-by and --skip-tests contradict each other" 2
    if kd_has_stage gate; then
      kd_die "--gated-by says the gate ran outside this run: leave the gate stage out (e.g. --only build)" 2
    fi
  fi
  if kd_has_stage build && ! kd_has_stage gate && ((!KD_SKIP_TESTS)) && [[ -z $KD_GATED_BY ]]; then
    kd_die "this run builds without the gate stage, and untested code does not ship. Add the gate stage, pass --gated-by=WHO when an outer pipeline (the deployer) ran the gate on this tree, or --skip-tests=REASON in an emergency" 2
  fi
}

# kd_gate_stage: the gate, its --skip-tests banner, or who ran it instead.
kd_gate_stage() {
  if kd_has_stage gate; then
    kd_phase "gate"
    kd_run_gate
  elif kd_has_stage build; then
    kd_phase "gate"
    if ((KD_SKIP_TESTS)); then
      kd_run_gate
    else
      kd_log "gate: run by $KD_GATED_BY on the tree being built (--gated-by); not repeated here"
    fi
  fi
}

kd_run_gate() {
  local -a args=()
  if ((KD_SKIP_TESTS)); then
    printf '%s\n' '!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!' \
      '!! --skip-tests: DEPLOYING CODE THE PRE-DEPLOY GATE HAS NOT TESTED.' \
      "!! Reason: ${KD_SKIP_TESTS_REASON:-none given (pass --skip-tests=<reason>)}" \
      "!! Run bash $PREDEPLOY_GATE_SUITE before the next deploy." \
      '!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!'
    return 0
  fi
  read -ra args <<< "$PREDEPLOY_GATE_ARGS"
  kd_run bash "$KD_DEPLOY_DIR/predeploy-gate.sh" --site-root "$SITE_ROOT" --suite "$PREDEPLOY_GATE_SUITE" -- "${args[@]}" \
    || kd_die "the pre-deploy gate failed; nothing was built or deployed" 1
}

kd_build_image() {  # IMAGE
  local image=$1 rev="<git HEAD>" created
  if ((!KD_DRY_RUN)); then
    rev=$(git -C "$SITE_ROOT" rev-parse HEAD 2>/dev/null) || rev=unknown
  fi
  created=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  kd_run docker build --platform "$DOCKER_PLATFORM" --file "$SITE_ROOT/$DOCKERFILE" --tag "$image" \
    --label "org.opencontainers.image.revision=$rev" --label "org.opencontainers.image.created=$created" \
    --label "affiliate-kit.app=$APP_NAME" --label "affiliate-kit.version=$KD_KIT_VERSION" "$SITE_ROOT" \
    || kd_die "docker build failed" 1
}

kd_push_image() {  # IMAGE LATEST
  kd_run docker push "$1" || kd_die "docker push $1 failed" 1
  kd_run docker tag "$1" "$2" || kd_die "docker tag failed" 1
  kd_run docker push "$2" || kd_die "docker push $2 failed" 1
}

kd_run_migrations() {  # IMAGE OWNER_DSN_REF
  local image=$1 ref=$2 dsn=""
  local -a words=()
  if [[ -z $MIGRATE_COMMAND ]]; then
    kd_log "migrate: MIGRATE_COMMAND is empty; skipped"
    return 0
  fi
  [[ -n $ref ]] || kd_die "MIGRATE_COMMAND is set but no owner DSN is: migrations run as the owner role" 2
  # Migrations run as this site's owner role on this site's database, never as
  # whatever role or database the reference happens to name.
  if kd_secret_get "$ref" dsn; then
    "$KD_PYTHON" -I "$KD_TOOL" dsn-info --dsn-file - --expect-user "$DB_OWNER_ROLE" --expect-db "$DB_NAME" \
      --label "the owner DSN ($ref)" <<< "$dsn" >/dev/null \
      || kd_die "migrations run only as $DB_OWNER_ROLE on database $DB_NAME: fix the owner DSN reference in $KD_ENV_FILE" 2
  fi
  read -ra words <<< "$MIGRATE_COMMAND"
  kd_run_with_secret_env DATABASE_URL "$ref" docker run --rm -e DATABASE_URL "$image" "${words[@]}" \
    || kd_die "migrations failed; nothing was released" 1
}

kd_smoke() {  # LABEL URL CONNECT_TO OPTIONAL(0|1)
  local p
  local -a args=(smoke --label "$1" --url "$2" --max-shared-age "$HTML_MAX_S_MAXAGE"
    --timeout "$SMOKE_TIMEOUT_SECONDS" --user-agent "$KD_SMOKE_UA")
  [[ -n $3 ]] && args+=(--connect-to "$3")
  (($4)) && args+=(--optional)
  for p in "${KD_SMOKE_PATHS[@]}"; do args+=(--path "$p"); done
  kd_run "$KD_PYTHON" -I "$KD_TOOL" "${args[@]}"
}

kd_banner() {
  if ((KD_DRY_RUN)); then
    printf '[dry-run] affiliate-kit deploy %s for %s: commands are printed, not run\n' "$1" "${APP_NAME:-?}"
  fi
}
