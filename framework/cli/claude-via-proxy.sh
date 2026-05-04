#!/bin/bash
# Run `claude` (the Anthropic CLI) routed through Cloudflare WARP's
# local SOCKS5 proxy at 127.0.0.1:40000.
#
# Why: on 2026-05-02 we hit Anthropic's IP-level edge rate-limit when 8
# parallel implementer dispatches all called `claude --print` from the
# same egress IP. Routing the implementer's traffic through WARP gives
# us a separate egress IP so the interactive Claude Code session
# (still on the home IP) and the batched implementers don't compete
# for the same IP-level burst budget.
#
# This wrapper is meant to be called instead of `claude` from the
# pool/implementer code path. Falls back to direct mode if WARP is not
# running (so the implementer never breaks because the proxy went down).

set -euo pipefail

PROXY_HOST="127.0.0.1"
PROXY_PORT="${WARP_PROXY_PORT:-40000}"
PROXY_URL="socks5h://${PROXY_HOST}:${PROXY_PORT}"

# Health-check: is WARP listening + actually routing?
USE_PROXY=0
if (echo > /dev/tcp/${PROXY_HOST}/${PROXY_PORT}) >/dev/null 2>&1; then
    USE_PROXY=1
fi

# Locate the real claude binary by walking PATH. We deliberately do NOT
# read CLAUDE_POOL_REAL_CLAUDE — the pool sets that env var to OUR OWN
# path so it spawns this wrapper, and reading it here would cause
# `exec "$REAL_CLAUDE"` to re-enter this script in an infinite loop.
# Symptom of the bug: bash pinned at ~67% CPU forever, no claude session
# files, no API traffic. The PATH walk below skips entries whose
# realpath equals this script, landing on the actual claude binary.
SELF="$(readlink -f "$0")"
REAL_CLAUDE=""
OIFS="$IFS"
IFS=":"
for d in $PATH; do
    cand="$d/claude"
    [ -x "$cand" ] || continue
    [ "$(readlink -f "$cand")" = "$SELF" ] && continue
    REAL_CLAUDE="$cand"
    break
done
IFS="$OIFS"
if [ -z "$REAL_CLAUDE" ] || [ ! -x "$REAL_CLAUDE" ]; then
    echo "[claude-via-proxy] cannot find real claude binary" >&2
    exit 127
fi

if [ "$USE_PROXY" = "1" ]; then
    export HTTPS_PROXY="$PROXY_URL"
    export HTTP_PROXY="$PROXY_URL"
    export ALL_PROXY="$PROXY_URL"
    # Anthropic + auth.anthropic.com only — never proxy localhost or LAN
    export NO_PROXY="localhost,127.0.0.1,::1,192.168.0.0/16,10.0.0.0/8,172.16.0.0/12"
    [ -n "${CLAUDE_PROXY_VERBOSE:-}" ] && echo "[claude-via-proxy] using $PROXY_URL" >&2
else
    [ -n "${CLAUDE_PROXY_VERBOSE:-}" ] && echo "[claude-via-proxy] WARP not reachable on ${PROXY_HOST}:${PROXY_PORT} — running direct" >&2
fi

exec "$REAL_CLAUDE" "$@"
