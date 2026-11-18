#!/usr/bin/env bash
# run-local-suite.sh: the affiliate-site kit's deploy-gate runner.
#
# Builds the site's production image (the one its Container App runs) from the
# tree being tested, starts it against the site's local fixture Postgres, waits
# for health, re-stamps the fixture's Amazon check times, and runs the
# Playwright gate (the kit's specs plus the site's) against it. Never touches
# production. Ported from SpecPicks tests/run-local-suite.sh; everything that
# was a literal there comes from the site's suite.env here.
#
#   bash tests/run-local-suite.sh [--suite-env FILE] [PLAYWRIGHT ARGS...]
#   bash tests/run-local-suite.sh --print-config      # resolved settings, then exit 0
#   KEEP_UP=1 bash tests/run-local-suite.sh           # leave the app container running
#
# A site's own tests/run-local-suite.sh is a shim that execs this file from
# vendor/affiliate-kit with its tests/suite.env (SUITE_ENV may name another).
# The deployer runs `bash run-local-suite.sh --workers=N` with cwd tests/ and
# appends --reporter/--output; every argument this script does not own goes
# to Playwright unchanged.
#
# Sharing the host. One run at a time per project: the flock on
# $AGENT_LOCK_DIR/local-suite-<SUITE_PROJECT>.lock, the lock SpecPicks' runner
# takes for itself. SUITE_HOST_LOCKS names other projects whose gate this one
# must never overlap; their locks are held for the whole run. A named project's
# lock file is created when missing, so the exclusion holds even before that
# gate has ever run (the lock directory is on tmpfs and empty after a reboot).
# '*' adds every local-suite-*.lock that exists at the moment the locks are
# taken: best effort on its own, so name the projects that matter and keep '*'
# as an addition ('other-site another-site *'). The locks are taken all or
# nothing: when one is busy, every lock taken so far is released and the set
# is tried again after SUITE_LOCK_POLL_S, so a waiting run never blocks a gate
# it does not conflict with; a waiting run prints a line a minute so the
# dispatcher's silence check does not kill it.
#
# CPU. The run is niced (SUITE_NICE), which reaches this script's children
# (Playwright, Chromium), and the app container is capped with docker update
# --cpus (SUITE_CPUS) once it starts. Containers and image builds run under
# dockerd, which nice does not reach: a site's compose files should also set
# `cpus: ${SUITE_CPUS:-0}` on the app and fixture-DB services so the cap holds
# from creation (the fixture site does), and BuildKit builds are not capped.
#
# Settings (suite.env is bash, sourced; an environment variable of the same
# name wins over it, so `SUITE_WORKERS=1 bash run-local-suite.sh` works):
#   SUITE_PROJECT            required; lock name and compose project, [a-z0-9_-]
#   SUITE_APP_PORT           required; host port the app under test listens on
#   SUITE_APP_HOST           host the gate reaches it at (default localhost)
#   SUITE_SITE_ROOT          site root, relative to suite.env's directory (default ..)
#   SUITE_TESTS_DIR          where Playwright runs (its playwright.config.ts), relative to suite.env's directory (default .)
#   SUITE_COMPOSE_FILES      compose files, relative to the site root (default "docker-compose.yml docker-compose.test.yml")
#   SUITE_APP_SERVICE        the app's compose service (default backend-test)
#   SUITE_APP_CONTAINER      its container name (default <project>_backend_test)
#   SUITE_DB_SERVICE         the fixture DB's compose service (default postgres)
#   SUITE_DB_CONTAINER       its container name (default <project>_postgres)
#   SUITE_DB_PORT/NAME/USER/PASSWORD   the fixture DB (fixture credentials, never secrets); gives the specs KIT_GATE_DB_URL
#   SUITE_FIXTURE_FRESHEN_SQL  SQL files (site-root relative) applied once the app is healthy
#   SUITE_WORKERS            Playwright workers unless the arguments name them (default 4)
#   SUITE_HOST_LOCKS         other projects' gate locks to hold (names; '*' adds every one present)
#   SUITE_NICE               niceness increment for the run (default 10; 0 = off)
#   SUITE_CPUS               CPU cap on the app container (default 2; 0 or empty = off; exported for compose `cpus:`)
#   SUITE_HEALTH_PATH        default /health
#   SUITE_HEALTH_TIMEOUT_S   default 240
#   SUITE_HEALTH_INTERVAL_S  default 2
#   SUITE_DB_WAIT_S          default 60
#   SUITE_LOCK_WAIT_S        default 3600
#   SUITE_LOCK_POLL_S        default 15
#   SUITE_NODE_MODULES       node_modules a clean worktree borrows from GATE_SOURCE_REPO (site-root relative; default node_modules)
# Environment only: SUITE_ENV, AGENT_LOCK_DIR (default /tmp/agent-framework-locks),
# GATE_SOURCE_REPO (the live checkout owning the fixture DB), GATE_ARTIFACT_DIR
# (image-under-test.txt, container.log on failure), KEEP_UP.
#
# Exit codes: Playwright's own (0 pass, 1 tests failed), or
#   2 fixture DB not up, 3 image does not build, 4 container did not start,
#   5 container exited before healthy, 6 never became healthy,
#   7 gave up waiting for another run, 8 the freshen SQL failed (it runs after
#   the commit's own migrations, so this is usually the commit),
#   78 bad settings (EX_CONFIG), 130 interrupted, 143 terminated.
set -uo pipefail

EX_DB=2 EX_BUILD=3 EX_START=4 EX_EXITED=5 EX_UNHEALTHY=6 EX_LOCK=7 EX_FRESHEN=8 EX_CONFIG=78

say() { printf '[suite] %s\n' "$*"; }
die_config() { printf '[suite] config error: %s\n' "$*" >&2; exit "$EX_CONFIG"; }

SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/$(basename "${BASH_SOURCE[0]}")"
ORIG_ARGS=("$@")

# ── arguments ────────────────────────────────────────────────────────────
SUITE_ENV_FILE="${SUITE_ENV:-}"
PRINT_CONFIG=0
PW_ARGS=()
while (($#)); do
    case $1 in
        --suite-env) (($# >= 2)) || die_config "--suite-env needs a file"; SUITE_ENV_FILE=$2; shift 2 ;;
        --suite-env=*) SUITE_ENV_FILE=${1#*=}; shift ;;
        --print-config) PRINT_CONFIG=1; shift ;;
        -h|--help) sed -n '2,/^set -uo/{/^#/{s/^# \{0,1\}//;p;};}' "$SELF"; exit 0 ;;
        *) PW_ARGS+=("$1"); shift ;;
    esac
done
[[ -n $SUITE_ENV_FILE ]] || die_config "no suite.env: pass --suite-env FILE or set SUITE_ENV"
[[ -f $SUITE_ENV_FILE ]] || die_config "suite.env not found: $SUITE_ENV_FILE"
SUITE_ENV_FILE="$(cd "$(dirname "$SUITE_ENV_FILE")" && pwd -P)/$(basename "$SUITE_ENV_FILE")"
SUITE_ENV_DIR="$(dirname "$SUITE_ENV_FILE")"

# ── settings: suite.env, with the environment winning ────────────────────
KEYS=(SUITE_PROJECT SUITE_APP_PORT SUITE_APP_HOST SUITE_SITE_ROOT SUITE_TESTS_DIR SUITE_COMPOSE_FILES
      SUITE_APP_SERVICE SUITE_APP_CONTAINER SUITE_DB_SERVICE SUITE_DB_CONTAINER SUITE_DB_PORT SUITE_DB_NAME
      SUITE_DB_USER SUITE_DB_PASSWORD SUITE_FIXTURE_FRESHEN_SQL SUITE_WORKERS SUITE_HOST_LOCKS SUITE_NICE
      SUITE_CPUS SUITE_HEALTH_PATH SUITE_HEALTH_TIMEOUT_S SUITE_HEALTH_INTERVAL_S SUITE_DB_WAIT_S
      SUITE_LOCK_WAIT_S SUITE_LOCK_POLL_S SUITE_NODE_MODULES)
declare -A FROM_ENV=()
for k in "${KEYS[@]}"; do
    if [[ -n ${!k+x} ]]; then FROM_ENV[$k]=${!k}; fi
done
# shellcheck disable=SC1090
source "$SUITE_ENV_FILE" || die_config "sourcing $SUITE_ENV_FILE failed"
for k in "${!FROM_ENV[@]}"; do printf -v "$k" '%s' "${FROM_ENV[$k]}"; done

: "${SUITE_PROJECT:=}" "${SUITE_APP_PORT:=}"
[[ $SUITE_PROJECT =~ ^[a-z0-9][a-z0-9_-]{0,62}$ ]] || die_config "SUITE_PROJECT must match [a-z0-9][a-z0-9_-]* (got '${SUITE_PROJECT}')"
is_port() { [[ $1 =~ ^[0-9]{1,5}$ ]] && (($1 >= 1 && $1 <= 65535)); }
is_uint() { [[ $1 =~ ^[0-9]{1,7}$ ]]; }
is_port "$SUITE_APP_PORT" || die_config "SUITE_APP_PORT must be a port (got '${SUITE_APP_PORT}')"
SUITE_APP_HOST=${SUITE_APP_HOST:-localhost}
[[ $SUITE_APP_HOST =~ ^[A-Za-z0-9.:-]+$ ]] || die_config "SUITE_APP_HOST is not a host name"
SUITE_SITE_ROOT=${SUITE_SITE_ROOT:-..}
SUITE_TESTS_DIR=${SUITE_TESTS_DIR:-.}
SUITE_COMPOSE_FILES=${SUITE_COMPOSE_FILES:-docker-compose.yml docker-compose.test.yml}
SUITE_APP_SERVICE=${SUITE_APP_SERVICE:-backend-test}
SUITE_APP_CONTAINER=${SUITE_APP_CONTAINER:-${SUITE_PROJECT}_backend_test}
SUITE_DB_SERVICE=${SUITE_DB_SERVICE:-postgres}
SUITE_DB_CONTAINER=${SUITE_DB_CONTAINER:-${SUITE_PROJECT}_postgres}
SUITE_DB_PORT=${SUITE_DB_PORT:-}
SUITE_DB_NAME=${SUITE_DB_NAME:-}
SUITE_DB_USER=${SUITE_DB_USER:-}
SUITE_DB_PASSWORD=${SUITE_DB_PASSWORD:-}
SUITE_FIXTURE_FRESHEN_SQL=${SUITE_FIXTURE_FRESHEN_SQL:-}
SUITE_WORKERS=${SUITE_WORKERS:-4}
SUITE_HOST_LOCKS=${SUITE_HOST_LOCKS:-}
SUITE_NICE=${SUITE_NICE:-10}
SUITE_CPUS=${SUITE_CPUS-2}
SUITE_HEALTH_PATH=${SUITE_HEALTH_PATH:-/health}
SUITE_HEALTH_TIMEOUT_S=${SUITE_HEALTH_TIMEOUT_S:-240}
SUITE_HEALTH_INTERVAL_S=${SUITE_HEALTH_INTERVAL_S:-2}
SUITE_DB_WAIT_S=${SUITE_DB_WAIT_S:-60}
SUITE_LOCK_WAIT_S=${SUITE_LOCK_WAIT_S:-3600}
SUITE_LOCK_POLL_S=${SUITE_LOCK_POLL_S:-15}
SUITE_NODE_MODULES=${SUITE_NODE_MODULES:-node_modules}

for k in SUITE_WORKERS SUITE_HEALTH_TIMEOUT_S SUITE_HEALTH_INTERVAL_S SUITE_DB_WAIT_S SUITE_LOCK_WAIT_S SUITE_LOCK_POLL_S; do
    is_uint "${!k}" || die_config "$k must be a whole number (got '${!k}')"
done
((SUITE_WORKERS >= 1)) || die_config "SUITE_WORKERS must be at least 1"
((SUITE_LOCK_POLL_S >= 1)) || die_config "SUITE_LOCK_POLL_S must be at least 1"
[[ $SUITE_NICE =~ ^[0-9]{1,2}$ ]] && ((SUITE_NICE <= 19)) || die_config "SUITE_NICE must be 0..19 (got '${SUITE_NICE}')"
[[ -z $SUITE_CPUS || $SUITE_CPUS =~ ^[0-9]+(\.[0-9]+)?$ ]] || die_config "SUITE_CPUS must be a number of CPUs (got '${SUITE_CPUS}')"
[[ $SUITE_HEALTH_PATH == /* ]] || die_config "SUITE_HEALTH_PATH must start with /"
for k in SUITE_APP_SERVICE SUITE_APP_CONTAINER SUITE_DB_SERVICE SUITE_DB_CONTAINER; do
    [[ ${!k} =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || die_config "$k is not a docker name (got '${!k}')"
done
if [[ -n $SUITE_DB_PORT ]]; then is_port "$SUITE_DB_PORT" || die_config "SUITE_DB_PORT must be a port"; fi
# Word lists, split without globbing ('*' in SUITE_HOST_LOCKS is a value, not a pattern).
read -r -a HOST_LOCKS <<< "$SUITE_HOST_LOCKS"
read -r -a COMPOSE_FILES <<< "$SUITE_COMPOSE_FILES"
read -r -a FRESHEN_SQL <<< "$SUITE_FIXTURE_FRESHEN_SQL"
for p in "${HOST_LOCKS[@]+"${HOST_LOCKS[@]}"}"; do
    [[ $p == '*' || $p =~ ^[a-z0-9][a-z0-9_-]{0,62}$ ]] || die_config "SUITE_HOST_LOCKS entry '$p' is not a project name or '*'"
done

SITE_ROOT="$(cd "$SUITE_ENV_DIR/$SUITE_SITE_ROOT" 2>/dev/null && pwd -P)" || die_config "SUITE_SITE_ROOT $SUITE_SITE_ROOT does not exist"
TESTS_DIR="$(cd "$SUITE_ENV_DIR/$SUITE_TESTS_DIR" 2>/dev/null && pwd -P)" || die_config "SUITE_TESTS_DIR $SUITE_TESTS_DIR does not exist"
COMPOSE_ARGS=()
for f in "${COMPOSE_FILES[@]+"${COMPOSE_FILES[@]}"}"; do
    [[ $f != /* && $f != *..* ]] || die_config "compose file $f must be a path inside the site root"
    [[ -f $SITE_ROOT/$f ]] || die_config "compose file not found: $SITE_ROOT/$f"
    COMPOSE_ARGS+=(-f "$SITE_ROOT/$f")
done
((${#COMPOSE_ARGS[@]})) || die_config "SUITE_COMPOSE_FILES names no file"
FRESHEN_FILES=()
for f in "${FRESHEN_SQL[@]+"${FRESHEN_SQL[@]}"}"; do
    [[ $f != /* && $f != *..* ]] || die_config "freshen SQL $f must be a path inside the site root"
    [[ -f $SITE_ROOT/$f ]] || die_config "freshen SQL not found: $SITE_ROOT/$f"
    FRESHEN_FILES+=("$SITE_ROOT/$f")
done
if ((${#FRESHEN_FILES[@]})) && [[ -z $SUITE_DB_USER || -z $SUITE_DB_NAME ]]; then
    die_config "SUITE_FIXTURE_FRESHEN_SQL needs SUITE_DB_USER and SUITE_DB_NAME"
fi

# The live checkout that owns the long-lived fixture DB. From a clean worktree
# the DB is never (re)created there: its compose project lives in the live tree.
SITE_REL="$(git -C "$SITE_ROOT" rev-parse --show-prefix 2>/dev/null || true)"
if [[ -n ${GATE_SOURCE_REPO:-} ]]; then
    SOURCE_SITE_ROOT="${GATE_SOURCE_REPO%/}/${SITE_REL}"
    SOURCE_SITE_ROOT="${SOURCE_SITE_ROOT%/}"
else
    SOURCE_SITE_ROOT="$SITE_ROOT"
fi
URL="http://${SUITE_APP_HOST}:${SUITE_APP_PORT}"
DB_URL=""
if [[ -n $SUITE_DB_PORT && -n $SUITE_DB_USER && -n $SUITE_DB_NAME ]]; then
    DB_URL="postgresql://${SUITE_DB_USER}:${SUITE_DB_PASSWORD}@127.0.0.1:${SUITE_DB_PORT}/${SUITE_DB_NAME}"
fi
LOCK_DIR="${AGENT_LOCK_DIR:-/tmp/agent-framework-locks}"
ARTIFACTS="${GATE_ARTIFACT_DIR:-}"

# Playwright workers: the suite's unless the arguments name their own.
HAS_WORKERS=0
for a in "${PW_ARGS[@]+"${PW_ARGS[@]}"}"; do
    case $a in --workers|--workers=*|-j|-j*) HAS_WORKERS=1 ;; esac
done
((HAS_WORKERS)) || PW_ARGS=("--workers=${SUITE_WORKERS}" "${PW_ARGS[@]+"${PW_ARGS[@]}"}")

if ((PRINT_CONFIG)); then
    printf 'suite_env=%s\nproject=%s\nsite_root=%s\nsource_site_root=%s\ntests_dir=%s\nurl=%s\n' \
        "$SUITE_ENV_FILE" "$SUITE_PROJECT" "$SITE_ROOT" "$SOURCE_SITE_ROOT" "$TESTS_DIR" "$URL"
    printf 'compose_files=%s\napp_service=%s\napp_container=%s\ndb_service=%s\ndb_container=%s\n' \
        "$SUITE_COMPOSE_FILES" "$SUITE_APP_SERVICE" "$SUITE_APP_CONTAINER" "$SUITE_DB_SERVICE" "$SUITE_DB_CONTAINER"
    printf 'db_url=%s\nfreshen=%s\nworkers=%s\nhost_locks=%s\nnice=%s\ncpus=%s\nlock_dir=%s\nplaywright_args=%s\n' \
        "${DB_URL/:${SUITE_DB_PASSWORD}@/:***@}" "$SUITE_FIXTURE_FRESHEN_SQL" "$SUITE_WORKERS" "$SUITE_HOST_LOCKS" \
        "$SUITE_NICE" "${SUITE_CPUS:-off}" "$LOCK_DIR" "${PW_ARGS[*]}"
    exit 0
fi

# ── nice: re-run this script under nice, once ────────────────────────────
if ((SUITE_NICE > 0)) && [[ -z ${KIT_SUITE_NICED:-} ]] && command -v nice >/dev/null 2>&1; then
    export KIT_SUITE_NICED=1
    exec nice -n "$SUITE_NICE" "$BASH" "$SELF" "${ORIG_ARGS[@]+"${ORIG_ARGS[@]}"}"
fi

# Compose files, the specs and the Playwright config may read the settings.
for k in "${KEYS[@]}"; do export "$k"; done
export TEST_URL="$URL" KIT_SUITE_PROJECT="$SUITE_PROJECT" KIT_SUITE_ENV="$SUITE_ENV_FILE"
if [[ -n $DB_URL ]]; then export KIT_GATE_DB_URL="$DB_URL"; fi

COMPOSE=(docker compose -p "$SUITE_PROJECT" --project-directory "$SITE_ROOT" "${COMPOSE_ARGS[@]}")

# ── locks ────────────────────────────────────────────────────────────────
LOCK_FDS=()
# The redirections that close every lock fd.
lock_closes() {
    local closes="" fd
    for fd in "${LOCK_FDS[@]+"${LOCK_FDS[@]}"}"; do closes+=" ${fd}>&-"; done
    printf '%s' "$closes"
}
# Run a command with every lock fd closed, so nothing it leaves behind holds a lock.
nolock() {
    eval '"$@"'"$(lock_closes)"
}

# Every lock this run must hold, sorted and unique: its own, each named host
# project's (created when missing), and for '*' every gate lock present now.
lock_set() {
    local f p
    local -a files=("$LOCK_DIR/local-suite-${SUITE_PROJECT}.lock")
    for p in "${HOST_LOCKS[@]+"${HOST_LOCKS[@]}"}"; do
        if [[ $p == '*' ]]; then
            for f in "$LOCK_DIR"/local-suite-*.lock; do [[ -e $f ]] && files+=("$f"); done
        else
            files+=("$LOCK_DIR/local-suite-${p}.lock")
        fi
    done
    printf '%s\n' "${files[@]}" | LC_ALL=C sort -u
}

release_locks() {
    local fd
    for fd in "${LOCK_FDS[@]+"${LOCK_FDS[@]}"}"; do exec {fd}>&-; done
    LOCK_FDS=()
}

# One attempt at the whole set, without waiting: on a busy lock, release every
# lock taken so far (BUSY_LOCK names it) and fail.
LOCK_SET=()
BUSY_LOCK=""
try_locks() {
    local f fd
    BUSY_LOCK=""
    mapfile -t LOCK_SET < <(lock_set)
    for f in "${LOCK_SET[@]}"; do
        exec {fd}>>"$f" || { say "cannot open lock file $f"; release_locks; exit "$EX_LOCK"; }
        LOCK_FDS+=("$fd")
        if ! flock -n "$fd"; then
            BUSY_LOCK=$f
            release_locks
            return 1
        fi
    done
    return 0
}

acquire_locks() {
    mkdir -p "$LOCK_DIR" || { say "cannot create $LOCK_DIR"; exit "$EX_LOCK"; }
    local start deadline last_note=0
    start=$(date +%s)
    deadline=$((start + SUITE_LOCK_WAIT_S))
    until try_locks; do
        if (( $(date +%s) >= deadline )); then
            say "gave up waiting for the suite lock $BUSY_LOCK after $(( $(date +%s) - start ))s"
            exit "$EX_LOCK"
        fi
        if ((last_note == 0)); then
            say "another run holds $BUSY_LOCK; waiting up to $((deadline - $(date +%s)))s (holding no lock meanwhile)"
            last_note=$(date +%s)
        elif (( $(date +%s) - last_note >= 60 )); then
            say "still waiting for the suite lock $BUSY_LOCK ($(( $(date +%s) - start ))s)"
            last_note=$(date +%s)
        fi
        sleep "$SUITE_LOCK_POLL_S"
    done
    say "holding $(printf '%s ' "${LOCK_SET[@]##*/}")"
}

# ── cleanup ──────────────────────────────────────────────────────────────
SUITE_RC=0
CHILD_PID=""
LINKED_NODE_MODULES=""
STARTED_APP=0
cleanup() {
    if [[ -n $CHILD_PID ]]; then
        kill -TERM "$CHILD_PID" 2>/dev/null
        wait "$CHILD_PID" 2>/dev/null
    fi
    if [[ -n $ARTIFACTS && $SUITE_RC != 0 && $STARTED_APP == 1 ]]; then
        mkdir -p "$ARTIFACTS"
        nolock docker logs --tail 400 "$SUITE_APP_CONTAINER" > "$ARTIFACTS/container.log" 2>&1
    fi
    if [[ $STARTED_APP == 1 ]]; then
        if [[ ${KEEP_UP:-0} != 1 ]]; then
            say "stopping $SUITE_APP_SERVICE"
            nolock "${COMPOSE[@]}" stop "$SUITE_APP_SERVICE" >/dev/null 2>&1
            nolock "${COMPOSE[@]}" rm -f "$SUITE_APP_SERVICE" >/dev/null 2>&1
            nolock docker rm -f "$SUITE_APP_CONTAINER" >/dev/null 2>&1
        else
            say "KEEP_UP=1: leaving ${URL} running"
        fi
    fi
    if [[ -n $LINKED_NODE_MODULES ]]; then
        rm -f "$LINKED_NODE_MODULES"
    fi
}
trap cleanup EXIT
finish() { SUITE_RC="$1"; exit "$1"; }
# A TERM from the deployer's timeout must still run cleanup (and stop the child).
trap 'finish 143' TERM
trap 'finish 130' INT

# Run a long step in the background and wait for it, so TERM is handled at once
# (bash defers traps while a foreground child runs). The subshell execs the
# command, so CHILD_PID is the command itself and cleanup's TERM reaches it.
run_child() {
    ( eval 'exec "$@"'"$(lock_closes)" ) &
    CHILD_PID=$!
    wait "$CHILD_PID"
    local rc=$?
    CHILD_PID=""
    return "$rc"
}

acquire_locks

# ── the fixture DB ───────────────────────────────────────────────────────
db_state() { nolock docker inspect -f "{{.State.$1}}" "$SUITE_DB_CONTAINER" 2>/dev/null; }
db_health() { nolock docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$SUITE_DB_CONTAINER" 2>/dev/null; }

say "the fixture DB ($SUITE_DB_CONTAINER) must be up"
if [[ "$(db_state Running)" != "true" ]]; then
    say "$SUITE_DB_CONTAINER is not running; starting it from $SOURCE_SITE_ROOT"
    # The fixture DB's compose file is the first one (the test file only adds the app).
    SRC_COMPOSE=(docker compose -p "$SUITE_PROJECT" --project-directory "$SOURCE_SITE_ROOT" -f "$SOURCE_SITE_ROOT/${COMPOSE_FILES[0]}")
    run_child "${SRC_COMPOSE[@]}" up -d "$SUITE_DB_SERVICE" || { say "could not start $SUITE_DB_SERVICE"; finish "$EX_DB"; }
fi
waited=0
while :; do
    health="$(db_health)"
    running="$(db_state Running)"
    [[ $running == "true" && ( $health == "healthy" || $health == "none" ) ]] && break
    if [[ $(db_state Status) == "exited" || $(db_state Status) == "dead" ]]; then
        say "$SUITE_DB_CONTAINER exited (code $(db_state ExitCode)); last 20 log lines:"
        nolock docker logs --tail 20 "$SUITE_DB_CONTAINER" 2>&1 | sed 's/^/    /'
        finish "$EX_DB"
    fi
    if ((waited >= SUITE_DB_WAIT_S)); then
        say "$SUITE_DB_CONTAINER is not healthy after ${waited}s (running=${running:-unknown}, health=${health:-unknown})"
        finish "$EX_DB"
    fi
    sleep "$SUITE_HEALTH_INTERVAL_S"
    waited=$((waited + SUITE_HEALTH_INTERVAL_S))
    ((SUITE_HEALTH_INTERVAL_S > 0)) || waited=$((waited + 1))
done

# ── build ────────────────────────────────────────────────────────────────
say "building the production image from $SITE_ROOT"
if ! run_child "${COMPOSE[@]}" build "$SUITE_APP_SERVICE"; then
    say "BUILD FAILED: this is a real gate failure: the image that would be deployed does not build. Not running the suite."
    finish "$EX_BUILD"
fi

# ── start ────────────────────────────────────────────────────────────────
say "starting $SUITE_APP_SERVICE on ${URL}"
# A container of this name left by an older run, or created from another directory, would make `up` fail.
nolock docker rm -f "$SUITE_APP_CONTAINER" >/dev/null 2>&1
STARTED_APP=1
run_child "${COMPOSE[@]}" up -d --no-deps --force-recreate "$SUITE_APP_SERVICE" || { say "container did not start"; finish "$EX_START"; }
if [[ -n $SUITE_CPUS && $SUITE_CPUS != 0 && $SUITE_CPUS != 0.0 ]]; then
    if nolock docker update --cpus "$SUITE_CPUS" "$SUITE_APP_CONTAINER" >/dev/null 2>&1; then
        say "capped $SUITE_APP_CONTAINER at $SUITE_CPUS CPUs"
    else
        say "WARN: could not cap $SUITE_APP_CONTAINER at $SUITE_CPUS CPUs (docker update --cpus failed)"
    fi
fi

printf '[suite] waiting for %s%s ' "$URL" "$SUITE_HEALTH_PATH"
healthy=0
started=$(date +%s)
while :; do
    if nolock curl -fs --max-time 3 -o /dev/null "${URL}${SUITE_HEALTH_PATH}"; then
        echo "- up"
        healthy=1
        break
    fi
    # A container that has already died is never going to answer.
    if [[ "$(nolock docker inspect -f '{{.State.Running}}' "$SUITE_APP_CONTAINER" 2>/dev/null)" == "false" ]]; then
        echo "- container exited"
        say "last 40 log lines:"
        nolock docker logs --tail 40 "$SUITE_APP_CONTAINER" 2>&1 | sed 's/^/    /'
        finish "$EX_EXITED"
    fi
    if (( $(date +%s) - started >= SUITE_HEALTH_TIMEOUT_S )); then break; fi
    printf '.'
    sleep "$SUITE_HEALTH_INTERVAL_S"
done
if ((!healthy)); then
    echo
    say "never became healthy within ${SUITE_HEALTH_TIMEOUT_S}s"
    nolock docker logs --tail 40 "$SUITE_APP_CONTAINER" 2>&1 | sed 's/^/    /'
    finish "$EX_UNHEALTHY"
fi

IMAGE_ID="$(nolock docker inspect -f '{{.Image}}' "$SUITE_APP_CONTAINER" 2>/dev/null)"
say "image under test: ${IMAGE_ID:0:19}"
if [[ -n $ARTIFACTS ]]; then
    mkdir -p "$ARTIFACTS"
    printf '%s\n' "$IMAGE_ID" > "$ARTIFACTS/image-under-test.txt"
fi

# ── freshen the fixture ──────────────────────────────────────────────────
# After health: the app's migrations (including any the commit under test adds)
# have run, so the SQL may use them; nothing the app renders has been cached yet.
for f in "${FRESHEN_FILES[@]+"${FRESHEN_FILES[@]}"}"; do
    say "freshening the fixture: ${f#"$SITE_ROOT"/}"
    if ! nolock docker exec -i "$SUITE_DB_CONTAINER" psql -X -q -v ON_ERROR_STOP=1 -U "$SUITE_DB_USER" -d "$SUITE_DB_NAME" < "$f"; then
        say "the freshen SQL failed on $SUITE_DB_CONTAINER/$SUITE_DB_NAME (after the commit's migrations: a schema change it did not follow?)"
        finish "$EX_FRESHEN"
    fi
done

# ── node_modules for a clean worktree ────────────────────────────────────
# A clean worktree has none (gitignored). Borrow the live checkout's, only now:
# before the build it would have gone into the build context.
NM_DEST="$SITE_ROOT/$SUITE_NODE_MODULES"
NM_SRC="$SOURCE_SITE_ROOT/$SUITE_NODE_MODULES"
if [[ ! -e $NM_DEST && -d $NM_SRC && $NM_SRC != "$NM_DEST" ]]; then
    ln -s "$NM_SRC" "$NM_DEST" && LINKED_NODE_MODULES="$NM_DEST"
fi

# ── the gate ─────────────────────────────────────────────────────────────
say "running Playwright against ${URL} (${PW_ARGS[*]})"
cd "$TESTS_DIR" || finish "$EX_CONFIG"
run_child npx --no-install playwright test "${PW_ARGS[@]}"
rc=$?
say "playwright exit=${rc}"
finish "$rc"
