#!/usr/bin/env bash
# create-affiliate-site.sh: create a new Amazon Associates site repo on the
# affiliate-site kit.
#
#   bash install/create-affiliate-site.sh <slug> <domain> \
#        (--content-rule-sets L | --content-policy-waived REASON) [options]
#
#   <slug>    site id, Container App name and gate project: lowercase letters,
#             digits and '-', 2 to 29 characters, starting with a letter. Not
#             a slug the reusable-agents site registry already gives a site,
#             nor one with gate containers or a fixture volume on this host.
#   <domain>  the apex domain, without a scheme (example.com)
#
# The content gate is never cut (binding amendment 11), so the scaffold asks
# which one the site has, and refuses to guess:
#   --content-rule-sets L      comma-separated content-policy rule sets every
#                              rendered page and published article must pass
#                              (reusable-agents config/content-policy-config.json)
#   --content-policy-waived R  or: why this site needs none, in words (10 to
#                              300 characters); the gate prints it on every run
#
# Generates the repo from _template/affiliate-site/site: site.json (validated
# by the kit's own loader), package.json with the kit's exact pins and a
# lockfile, tsconfig, src/index.ts mounting the kit middleware and a route
# table, db/migrations (applied after the kit's), the deploy-gate wiring
# (tests/suite.env, tests/run-local-suite.sh, the gate policy and page
# manifest, a fixture), deploy.env and Dockerfile.azure rendered from the
# kit's templates, azure/ and aws/ wrappers, agents/register-with-framework.sh,
# CLAUDE.md, keep-the-lights-on.yaml and agents/implementer-scope.yaml. Vendors the kit
# with install/sync-site-kit.sh, and runs `git init` on branch main without a
# remote. Nothing is committed, registered, provisioned or deployed.
#
# The repo is assembled and checked in a scratch directory first and moved
# into place last: either the whole repo appears, or nothing does.
#
# Options:
#   --dir DIR               where to create it (default: <slug> next to this
#                           reusable-agents checkout), resolved to its real
#                           path first; must be missing or empty, and outside
#                           this checkout.
#   --dry-run               assemble and check everything in a scratch directory,
#                           print the plan, and write nothing to DIR
#   --brand NAME            the brand shown on the site (default: the slug, capitalised)
#   --byline NAME           the editorial byline (default: "<brand> Editorial Team")
#   --partner-tag TAG       the Amazon tracking ID (default: <slug>-20)
#   --niche NAME            the niche library's directory, src/<niche>/; its
#                           operator-owned policy is src/<niche>/policy.ts (default: niche)
#   --gate-port PORT        host port of the app under test in the deploy gate (default 10031)
#   --fixture-db-port PORT  host port of the gate's fixture Postgres (default 5433)
#   --host-locks NAMES      gate projects this site's gate must never overlap,
#                           comma- or space-separated; tests/suite.env holds
#                           their locks for the whole run (created when
#                           missing), plus '*' for any other gate lock present
#                           (default: every other enabled site in the
#                           reusable-agents site registry; '' for '*' alone)
#   --indexnow-key KEY      the public IndexNow key (default: 32 random hex digits)
#   --kit DIR               the kit to vendor (default: kits/affiliate-site here)
#   -h, --help              this text
#
# Needs bash 4.4+, python3, node 22+, git, realpath, and the kit's
# dependencies (npm ci in kits/affiliate-site): site.json is validated with
# the kit's code. docker is optional (when present, the slug's gate
# containers and fixture volume must not exist yet).
#
# Exit: 0 created (or planned); 1 a step failed (nothing was created);
#       2 usage error, DIR not empty, or the slug is taken;
#       3 a required tool is missing or the site registry cannot be read.
set -euo pipefail

SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/$(basename "${BASH_SOURCE[0]}")"
INSTALL_DIR="$(dirname "$SELF")"
REPO_ROOT="$(cd "$INSTALL_DIR/.." && pwd -P)"
TEMPLATE="$REPO_ROOT/_template/affiliate-site"
SCAFFOLD="$TEMPLATE/scaffold"

usage() { sed -n '2,/^set -euo/{/^#/{s/^# \{0,1\}//;p;};}' "$SELF"; }
say() { printf '[create-affiliate-site] %s\n' "$*"; }
die() { printf '[create-affiliate-site] error: %s\n' "$1" >&2; exit "${2:-1}"; }
warn() { printf '[create-affiliate-site] WARNING: %s\n' "$*" >&2; }

# ── arguments ────────────────────────────────────────────────────────────
SLUG="" DOMAIN="" DIR="" DRY_RUN=0 BRAND="" BYLINE="" PARTNER_TAG="" NICHE="niche"
GATE_PORT=10031 FIXTURE_DB_PORT=5433 RULE_SETS="" CONTENT_WAIVED="" INDEXNOW_KEY="" KIT="$REPO_ROOT/kits/affiliate-site"
HOST_LOCKS_SET=0 HOST_LOCKS_ARG=""
declare -a POSITIONAL=()
need_value() { (($1 >= 2)) || die "$2 needs a value" 2; }
while (($#)); do
    case $1 in
        --dir) need_value $# "$1"; DIR=$2; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        --brand) need_value $# "$1"; BRAND=$2; shift 2 ;;
        --byline) need_value $# "$1"; BYLINE=$2; shift 2 ;;
        --partner-tag) need_value $# "$1"; PARTNER_TAG=$2; shift 2 ;;
        --niche) need_value $# "$1"; NICHE=$2; shift 2 ;;
        --gate-port) need_value $# "$1"; GATE_PORT=$2; shift 2 ;;
        --fixture-db-port) need_value $# "$1"; FIXTURE_DB_PORT=$2; shift 2 ;;
        --content-rule-sets) need_value $# "$1"; RULE_SETS=$2; shift 2 ;;
        --content-policy-waived) need_value $# "$1"; CONTENT_WAIVED=$2; shift 2 ;;
        --host-locks) need_value $# "$1"; HOST_LOCKS_ARG=$2; HOST_LOCKS_SET=1; shift 2 ;;
        --indexnow-key) need_value $# "$1"; INDEXNOW_KEY=$2; shift 2 ;;
        --kit) need_value $# "$1"; KIT=$2; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        --) shift; POSITIONAL+=("$@"); break ;;
        -*) die "unknown option $1 (see --help)" 2 ;;
        *) POSITIONAL+=("$1"); shift ;;
    esac
done
((${#POSITIONAL[@]} == 2)) || { usage >&2; die "expected <slug> <domain>" 2; }
SLUG=${POSITIONAL[0]}
DOMAIN=${POSITIONAL[1]}

# ── validation (the same rules the kit's schema and deploy scripts apply) ─
[[ $SLUG =~ ^[a-z][a-z0-9-]{0,27}[a-z0-9]$ && $SLUG != *--* ]] \
    || die "slug '$SLUG': lowercase letters, digits and '-', 2-29 characters, starting with a letter, no '--'" 2
case $SLUG in
    pg-*|pg|azure*|rds*|postgres*|template*) die "slug '$SLUG' collides with reserved database names (pg, azure, rds, postgres, template)" 2 ;;
esac
[[ $DOMAIN =~ ^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$ && ${#DOMAIN} -le 253 ]] \
    || die "domain '$DOMAIN': a lowercase domain name without scheme or path (example.com)" 2
[[ $DOMAIN != www.* ]] || die "domain '$DOMAIN': give the apex domain; www.$DOMAIN is served too" 2
[[ -n $BRAND ]] || BRAND="$(tr '[:lower:]' '[:upper:]' <<< "${SLUG:0:1}")${SLUG:1}"
[[ -n $BYLINE ]] || BYLINE="$BRAND Editorial Team"
FREE_TEXT_RE="^[A-Za-z0-9][A-Za-z0-9 &'.,!:+-]*\$"
free_text_ok() { [[ $1 =~ $FREE_TEXT_RE ]] && [[ $1 != *'  '* && $1 != *' ' ]]; }
if ! free_text_ok "$BRAND" || ((${#BRAND} > 60)); then
    die "brand '$BRAND': 1-60 characters: letters, digits, spaces and & ' . , ! : + -" 2
fi
if ! free_text_ok "$BYLINE" || ((${#BYLINE} > 80)); then
    die "byline '$BYLINE': 1-80 characters: letters, digits, spaces and & ' . , ! : + -" 2
fi
[[ -n $PARTNER_TAG ]] || PARTNER_TAG="$SLUG-20"
[[ $PARTNER_TAG =~ ^[a-z0-9][a-z0-9-]{0,60}-[0-9]{2}$ ]] \
    || die "partner tag '$PARTNER_TAG': an Amazon tracking ID like $SLUG-20" 2
[[ $NICHE =~ ^[a-z][a-z0-9-]{0,39}$ ]] || die "niche '$NICHE': lowercase letters, digits and '-', starting with a letter" 2
case $NICHE in config|db|render|routes) die "niche '$NICHE' collides with a scaffold directory under src/" 2 ;; esac
for p in GATE_PORT:--gate-port FIXTURE_DB_PORT:--fixture-db-port; do
    v=${p%%:*}
    if ! [[ ${!v} =~ ^[0-9]{4,5}$ ]] || ((${!v} < 1024 || ${!v} > 65535)); then
        die "${p#*:}: a port from 1024 to 65535 (got '${!v}')" 2
    fi
done
[[ ${GATE_PORT} != "${FIXTURE_DB_PORT}" ]] || die "the gate port and the fixture DB port must differ" 2
RULE_SETS=${RULE_SETS// /}
[[ -z $RULE_SETS || $RULE_SETS =~ ^[A-Za-z0-9_.-]+(,[A-Za-z0-9_.-]+)*$ ]] \
    || die "--content-rule-sets '$RULE_SETS': comma-separated rule-set names" 2
# The content gate: rule sets, or the reason in words. Never an empty list.
if [[ -n $RULE_SETS && -n $CONTENT_WAIVED ]]; then
    die "give --content-rule-sets or --content-policy-waived, not both" 2
fi
if [[ -z $RULE_SETS && -z $CONTENT_WAIVED ]]; then
    die "choose the content gate: --content-rule-sets a,b (the rule sets every rendered page and article must pass; reusable-agents config/content-policy-config.json) or --content-policy-waived REASON (why this site needs none)" 2
fi
if [[ -n $CONTENT_WAIVED ]]; then
    WAIVER_RE='^[^[:cntrl:]]{10,300}$'
    if ! [[ $CONTENT_WAIVED =~ $WAIVER_RE && $CONTENT_WAIVED == *[[:alpha:]]*[[:alpha:]]* ]] \
        || [[ $CONTENT_WAIVED == *'{{'* || $CONTENT_WAIVED == *'}}'* || $CONTENT_WAIVED == ' '* || $CONTENT_WAIVED == *' ' ]]; then
        die "--content-policy-waived: the reason, in words: 10-300 characters on one line" 2
    fi
fi
# Other gates' projects (validated now; the registry default is read below).
declare -a HOST_LOCK_NAMES=()
if ((HOST_LOCKS_SET)); then
    read -r -a HOST_LOCK_NAMES <<< "${HOST_LOCKS_ARG//,/ }"
    for p in "${HOST_LOCK_NAMES[@]+"${HOST_LOCK_NAMES[@]}"}"; do
        [[ $p == '*' || $p =~ ^[a-z0-9][a-z0-9_-]{0,62}$ ]] || die "--host-locks: '$p' is not a gate project name" 2
    done
fi
[[ -z $INDEXNOW_KEY || $INDEXNOW_KEY =~ ^[A-Za-z0-9-]{8,128}$ ]] || die "IndexNow key: 8-128 characters of [A-Za-z0-9-]" 2

# ── tools ─────────────────────────────────────────────────────────────────
for t in python3 node git realpath; do command -v "$t" >/dev/null 2>&1 || die "$t is required" 3; done
# The IndexNow key is public by design (served at /<key>.txt); random so sites never share one.
[[ -n $INDEXNOW_KEY ]] || INDEXNOW_KEY="$(python3 -I -c 'import secrets; print(secrets.token_hex(16))')"
node_major="$(node -p 'process.versions.node.split(".")[0]')"
((node_major >= 22)) || die "node 22 or later is required (found $(node --version))" 3
[[ -d $TEMPLATE/site && -f $SCAFFOLD/render_template.py ]] || die "the template is missing: $TEMPLATE" 2
KIT="$(cd "$KIT" 2>/dev/null && pwd -P)" || die "kit directory not found" 2
[[ -f $KIT/VERSION && -f $KIT/deploy/lib/deploytool.py ]] || die "$KIT is not the affiliate-site kit" 2
KIT_VERSION="$(tr -d '[:space:]' < "$KIT/VERSION")"
if ! node -e "require.resolve('esbuild', { paths: [process.argv[1]] })" "$KIT" >/dev/null 2>&1; then
    die "the kit's dependencies are not installed; run: (cd $KIT && npm ci)" 3
fi

# Fixture databases are long-lived containers: two sites (or a site and the
# kit's own gate fixture) cannot share a port. Warn about one in use now.
for port in "$GATE_PORT" "$FIXTURE_DB_PORT"; do
    if python3 -I -c 'import socket,sys; s=socket.socket(); s.settimeout(0.5); sys.exit(0 if s.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)' "$port"; then
        printf '[create-affiliate-site] WARNING: something already listens on 127.0.0.1:%s; give this site gate ports no other gate or fixture uses (--gate-port, --fixture-db-port)\n' "$port" >&2
    fi
done

# ── target ────────────────────────────────────────────────────────────────
[[ -n $DIR ]] || DIR="$(dirname "$REPO_ROOT")/$SLUG"
# Canonical before any check: '..' and symbolic links in a parent resolve
# here, so the containment check below sees where the site really lands, and
# the path baked into CLAUDE.md, README, the implementer scope and the KTLO
# config is the real one.
DIR="$(realpath -m -- "$DIR")" || die "--dir: cannot resolve $DIR" 2
[[ $DIR != / ]] || die "--dir: / is not a site directory" 2
case $DIR in
    "$REPO_ROOT"|"$REPO_ROOT"/*) die "$DIR is inside the reusable-agents checkout; a site is its own repository" 2 ;;
esac
if [[ -e $DIR || -L $DIR ]]; then
    [[ -d $DIR && ! -L $DIR ]] || die "$DIR exists and is not a directory" 2
    [[ -z "$(ls -A "$DIR")" ]] || die "$DIR exists and is not empty; refusing to write into it" 2
fi
SAFE_PATH_RE='^[A-Za-z0-9._/@+,=-]+$'
[[ $DIR =~ $SAFE_PATH_RE ]] || die "$DIR: use a path of letters, digits and ._/@+,=- only" 2
PARENT="$(dirname "$DIR")"

# ── the slug is free on this fleet and host ──────────────────────────────
# The slug names the gate project, the fixture containers (the gate runner
# removes and recreates <slug>_backend_test), the gate lock, the database,
# its roles and the Container App. A registered site keeps its own.
FLEET="$SCAFFOLD/fleet_sites.py"
fleet_rc=0
fleet_msg="$(python3 -I "$FLEET" check-slug --repo "$REPO_ROOT" --slug "$SLUG" --dir "$DIR" 2>&1)" || fleet_rc=$?
case $fleet_rc in
    0) ;;
    2) die "${fleet_msg#fleet_sites: }" 2 ;;
    *) die "cannot read the reusable-agents site registry, which the slug is checked against: ${fleet_msg#fleet_sites: }" 3 ;;
esac
if command -v docker >/dev/null 2>&1; then
    if containers="$(timeout 15 docker ps -a --format '{{.Names}}' 2>/dev/null)" \
        && volumes="$(timeout 15 docker volume ls --format '{{.Name}}' 2>/dev/null)"; then
        for c in "${SLUG}_postgres" "${SLUG}_backend_test"; do
            if grep -qxF -- "$c" <<< "$containers"; then
                die "a docker container named $c already exists on this host: another gate uses slug '$SLUG' (or a removed site left it: docker rm -f $c)" 2
            fi
        done
        if grep -qxF -- "${SLUG}_pgdata" <<< "$volumes"; then
            die "a docker volume named ${SLUG}_pgdata already exists on this host: the new gate's fixture database would reuse its data (a removed site's leftover: docker volume rm ${SLUG}_pgdata)" 2
        fi
    else
        warn "docker did not answer; could not check this host for existing ${SLUG}_postgres / ${SLUG}_backend_test containers"
    fi
fi

# ── the gates this one never overlaps ────────────────────────────────────
if ((! HOST_LOCKS_SET)); then
    names="$(python3 -I "$FLEET" gate-projects --repo "$REPO_ROOT" --except "$SLUG")" \
        || die "cannot read the reusable-agents site registry for the other gates' projects; pass --host-locks NAMES" 3
    read -r -a HOST_LOCK_NAMES <<< "$names"
fi
declare -a LOCKS=()
for p in "${HOST_LOCK_NAMES[@]+"${HOST_LOCK_NAMES[@]}"}"; do
    [[ $p != '*' && $p != "$SLUG" ]] || continue
    [[ " ${LOCKS[*]+"${LOCKS[*]}"} " == *" $p "* ]] || LOCKS+=("$p")
done
HOST_LOCKS="${LOCKS[*]+"${LOCKS[*]} "}*"

# ── scratch ───────────────────────────────────────────────────────────────
if ((DRY_RUN)); then
    STAGE="$(mktemp -d "${TMPDIR:-/tmp}/affiliate-site-plan.XXXXXX")"
else
    mkdir -p "$PARENT"
    STAGE="$(mktemp -d "$PARENT/.${SLUG}.scaffold.XXXXXX")"
fi
cleanup() { rm -rf "$STAGE"; }
trap cleanup EXIT
SITE="$STAGE/site"

DB_IDENT="${SLUG//-/_}"
ENV_IDENT="${DB_IDENT^^}"
POLICY_FILE="src/$NICHE/policy.ts"
CREATED_ON="$(date -u +%Y-%m-%d)"
say "assembling $SLUG ($DOMAIN) from kit $KIT_VERSION in a scratch directory"

# ── 1. render the template ───────────────────────────────────────────────
declare -a VARS=(
    --var "SLUG=$SLUG" --var "DOMAIN=$DOMAIN" --var "BRAND=$BRAND" --var "BYLINE=$BYLINE"
    --var "DISPLAY_NAME=$BRAND ($DOMAIN)"
    --var "DESCRIPTION=$BRAND ($DOMAIN): an Amazon Associates review site on the affiliate-site kit"
    --var "EARNINGS=As an Amazon Associate, $BRAND earns from qualifying purchases."
    --var "PARTNER_TAG=$PARTNER_TAG" --var "NICHE=$NICHE" --var "POLICY_FILE=$POLICY_FILE"
    --var "GATE_PORT=$GATE_PORT" --var "FIXTURE_DB_PORT=$FIXTURE_DB_PORT"
    --var "FIXTURE_DB_NAME=${DB_IDENT}_fixture" --var "FIXTURE_DB_USER=${DB_IDENT}_fixture"
    --var "FIXTURE_DB_PASSWORD=${DB_IDENT}_fixture"
    --var "CONTENT_RULE_SETS=$RULE_SETS" --var "CONTENT_POLICY_WAIVED=$CONTENT_WAIVED"
    --var "CONTENT_RULE_SETS_TEXT=${RULE_SETS:-none: the content gate is waived ($CONTENT_WAIVED)}"
    --var "HOST_LOCKS=$HOST_LOCKS"
    --var "INDEXNOW_KEY=$INDEXNOW_KEY" --var "KIT_VERSION=$KIT_VERSION" --var "SITE_DIR=$DIR"
    --var "ENV_IDENT=$ENV_IDENT" --var "CREATED_ON=$CREATED_ON"
)
python3 -I "$SCAFFOLD/render_template.py" --template "$TEMPLATE/site" --out "$SITE" "${VARS[@]}" > /dev/null \
    || die "rendering the template failed"

# ── 2. package.json pins and lockfile (before vendoring: the sync checks them) ─
python3 -I "$SCAFFOLD/site_files.py" package --kit "$KIT" --site "$SITE" > /dev/null \
    || die "writing package.json and package-lock.json failed"

# ── 3. vendor the kit ────────────────────────────────────────────────────
bash "$INSTALL_DIR/sync-site-kit.sh" "$SITE" --kit "$KIT" --quiet || die "vendoring the kit failed"
VENDOR="$SITE/vendor/affiliate-kit"
cp "$VENDOR/deploy/dockerignore.example" "$SITE/.dockerignore"
chmod 0644 "$SITE/.dockerignore"

# ── 4. deploy.env from the kit's documented contract ─────────────────────
declare -a SETS=(
    --set "APP_NAME=$SLUG" --set "SITE_DOMAIN=$DOMAIN" --set "SITE_HOSTNAMES=$DOMAIN www.$DOMAIN"
    --set "BUILD_SCRIPT=build:image" --set "DOCKER_EXTRA_PATHS=db content"
    --set "APP_SECRETS=database-url=env:DATABASE_URL_${ENV_IDENT}_APP"
    --set "DB_NAME=$DB_IDENT"
    --set "DB_OWNER_ROLE=${DB_IDENT}_owner" --set "DB_APP_ROLE=${DB_IDENT}_app"
    --set "DB_AGENTS_ROLE=${DB_IDENT}_agents" --set "DB_PUBLISHER_ROLE=${DB_IDENT}_publisher"
    --set "DB_OWNER_DSN=env:DATABASE_URL_${ENV_IDENT}_OWNER" --set "DB_APP_DSN=env:DATABASE_URL_${ENV_IDENT}_APP"
    --set "DB_AGENTS_DSN=env:DATABASE_URL_${ENV_IDENT}" --set "DB_PUBLISHER_DSN=env:DATABASE_URL_${ENV_IDENT}_PUBLISHER"
    --set "DB_PUBLISHER_GRANTS=SELECT,UPDATE(status,published_at):editorial_articles"
    --set "MIGRATE_COMMAND=node dist/vendor/affiliate-kit/server/src/db/migrate.js --site-dir db/migrations"
    # Only routes this scaffold serves: the smoke expects 200 from each, and a
    # failed smoke rolls a release back. Product and best-of pages join when
    # those routes land (tests/unit/deploy-config.test.ts checks the list).
    --set "SMOKE_PATHS=/ /health /robots.txt /llms.txt /sitemap.xml /guides /about"
    --set "AWS_SECRET_ID=nsc/$SLUG"
)
python3 -I "$SCAFFOLD/site_files.py" deploy-env --kit "$VENDOR" --site "$SITE" "${SETS[@]}" \
    --header-line "deploy.env for $DOMAIN: the contract of the kit's deploy scripts (vendor/affiliate-kit/deploy)." \
    --header-line "Generated by reusable-agents install/create-affiliate-site.sh from the kit's" \
    --header-line "deploy/deploy.env.example (kit $KIT_VERSION). References only; never a secret value." > /dev/null \
    || die "writing deploy.env failed"

# ── 5. Dockerfile.azure, rendered from the kit template with deploy.env's values ─
python3 -I "$VENDOR/deploy/lib/deploytool.py" env-dump --strict --check "$SITE/deploy.env" \
    || die "deploy.env does not parse as the kit's deploy scripts read it"
declare -A ENVV=()
while IFS= read -r -d '' k && IFS= read -r -d '' v; do ENVV[$k]=$v; done \
    < <(python3 -I "$VENDOR/deploy/lib/deploytool.py" env-dump --strict "$SITE/deploy.env")
for k in APP_NAME NODE_IMAGE TARGET_PORT SERVER_ENTRY BUILD_SCRIPT HEALTH_PATH DOCKER_EXTRA_PATHS DOCKERFILE; do
    [[ -n ${ENVV[$k]+x} ]] || die "deploy.env has no $k (the kit's deploy.env.example changed; update the scaffold)"
done
python3 -I "$VENDOR/deploy/lib/deploytool.py" render-dockerfile --template "$VENDOR/deploy/Dockerfile.azure.tmpl" \
    --app-name "${ENVV[APP_NAME]}" --kit-version "$KIT_VERSION" --node-image "${ENVV[NODE_IMAGE]}" \
    --port "${ENVV[TARGET_PORT]}" --server-entry "${ENVV[SERVER_ENTRY]}" --build-script "${ENVV[BUILD_SCRIPT]}" \
    --health-path "${ENVV[HEALTH_PATH]}" --extra-paths "${ENVV[DOCKER_EXTRA_PATHS]}" \
    --out "$SITE/${ENVV[DOCKERFILE]}" || die "rendering Dockerfile.azure failed"
chmod 0644 "$SITE/${ENVV[DOCKERFILE]}"

# ── 6. checks ────────────────────────────────────────────────────────────
summary="$(node "$SCAFFOLD/validate_site_json.cjs" --kit "$KIT" "$SITE/site.json")" \
    || die "site.json does not pass the kit's checks"
summary=${summary//"$SITE"/"$DIR"}
python3 -I "$SCAFFOLD/vendor_kit.py" verify --site "$SITE" --quiet || die "the vendored kit does not verify"
while IFS= read -r -d '' f; do
    bash -n "$f" || die "${f#"$SITE"/} is not valid bash"
done < <(find "$SITE" -name '*.sh' -not -path '*/vendor/*' -print0)
while IFS= read -r -d '' f; do
    python3 -I -c 'import json,sys; json.load(open(sys.argv[1]))' "$f" 2>/dev/null || die "${f#"$SITE"/} is not valid JSON"
done < <(find "$SITE" -name '*.json' -not -path '*/vendor/*' -print0)
if grep -rIl --exclude-dir=vendor -E '\{\{ *([a-z]+:)?[A-Za-z_][A-Za-z0-9_]* *\}\}' "$SITE" >/dev/null 2>&1; then
    die "a placeholder survived rendering: $(grep -rIl --exclude-dir=vendor -E '\{\{ *([a-z]+:)?[A-Za-z_][A-Za-z0-9_]* *\}\}' "$SITE" | head -3 | tr '\n' ' ')"
fi
git -C "$SITE" init -q -b main >/dev/null 2>&1 || git -C "$SITE" init -q >/dev/null || die "git init failed"
git -C "$SITE" symbolic-ref HEAD refs/heads/main
[[ -z "$(git -C "$SITE" remote)" ]] || die "the new repository has a remote; it must have none"

nfiles="$(find "$SITE" -type f -not -path '*/.git/*' | wc -l | tr -d ' ')"
if ((DRY_RUN)); then
    say "dry run: $DIR would hold $nfiles files (kit $KIT_VERSION vendored), git on main with no remote:"
    (cd "$SITE" && find . -type f -not -path './.git/*' -not -path './vendor/*' | sed 's|^\./||' | LC_ALL=C sort | sed 's/^/    /')
    say "    vendor/affiliate-kit/ ($(find "$VENDOR" -type f | wc -l | tr -d ' ') files)"
    say "site.json: $summary"
    say "nothing was written to $DIR"
    exit 0
fi

# ── 7. move into place ───────────────────────────────────────────────────
if [[ -d $DIR ]]; then rmdir "$DIR"; fi
mv "$SITE" "$DIR"
say "created $DIR: $nfiles files, kit $KIT_VERSION vendored, git on main (no remote, nothing committed)"
say "site.json: $summary"
say "gate: never overlaps ${HOST_LOCKS}; content gate: ${RULE_SETS:-waived}"
cat <<EOF
[create-affiliate-site] next:
    cd $DIR
    npm ci && npm run typecheck && npm test      # the lockfile pins the kit's dependencies
    bash tests/run-local-suite.sh                # the deploy gate (docker): app :$GATE_PORT, fixture DB :$FIXTURE_DB_PORT
    git add -A && git commit -m "Scaffold $SLUG on affiliate-kit $KIT_VERSION"
  then fill in site.json (segment tags), CLAUDE.md (niche safety boundary) and $POLICY_FILE,
  and read vendor/affiliate-kit/deploy/README.md before provisioning anything.
  Keep this site's implementer dispatches disabled: the implementer's claude path applies no
  path scope, so nothing yet keeps it out of vendor/, site.json, $POLICY_FILE or the gate policy
  (agents/README.md, "Prerequisite").
EOF
if [[ -n $RULE_SETS ]]; then
    cat <<EOF
  A content-policy site installs the kit's publish guard before its first article: until then an
  author's INSERT is published as written (vendor/affiliate-kit/migrations/README.md, "The publish guard").
EOF
fi
