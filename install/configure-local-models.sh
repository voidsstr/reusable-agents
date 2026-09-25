#!/usr/bin/env bash
# configure-local-models.sh — put the fleet host's GPU services on the
# one-resident-model design (decided 2026-09-23 from a blind benchmark).
#
#   LLM:   ONE Ollama model, qwen3.8:27b (text + vision), at ONE context size
#          (num_ctx 65536), called with think:false. Code defaults
#          live in framework/core/local_llm.py; this script sets the host side.
#   Image: local-image-gen on Tongyi-MAI/Z-Image-Turbo in low-VRAM mode
#          (fp8 transformer, text encoder on CPU, 9 steps), ~9 GB peak.
#
# What it does (each step is skipped when already in place):
#   1. /etc/systemd/system/ollama.service.d/10-models-dir.conf   (sudo tee)
#   2. FLEET_LOCAL_MODEL + OLLAMA_NUM_CTX in ~/.reusable-agents/secrets.env
#      (single-quoted, append-or-replace; the file is never printed)
#   3. ~/.config/systemd/user/local-image-gen.service.d/10-model.conf
#   4. ollama pull qwen3.8:27b
#   5. daemon-reload; restart ollama (sudo) and local-image-gen (user) when
#      their config changed (or with --restart); warm the model at the fleet
#      num_ctx
#   6. verification summary: ollama ps, :7861/healthz
#   +  /etc/systemd/system/nvidia-power-cap.service, enabled at boot: caps the
#      GPU at GPU_POWER_LIMIT_W (default 450 W). Uncapped at 575 W the host
#      hard-reset twice on 2026-09-24 under full inference load (no clean
#      shutdown in the journal) and earlier dropped the card off the bus
#      (Xid 79). 450 W costs little local-model throughput.
#
# Usage:
#   bash install/configure-local-models.sh             # converge
#   bash install/configure-local-models.sh --dry-run   # print what it would write/run
#   bash install/configure-local-models.sh --restart   # converge + restart both services
#
# Run it as the operator user (not root): it writes user units and
# ~/.reusable-agents/secrets.env, and uses sudo only for the ollama drop-in.

set -euo pipefail

DRY_RUN=0
FORCE_RESTART=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        --restart) FORCE_RESTART=1 ;;
        -h|--help) sed -n '2,29p' "$0"; exit 0 ;;
        *) echo "unknown argument: $arg (try --help)" >&2; exit 2 ;;
    esac
done

FLEET_LOCAL_MODEL="${FLEET_LOCAL_MODEL:-qwen3.8:27b}"
OLLAMA_NUM_CTX="${OLLAMA_NUM_CTX:-65536}"
OLLAMA_URL="http://127.0.0.1:11434"
IMAGE_GEN_URL="http://127.0.0.1:7861"
DEV_ROOT="${DEV_ROOT:-$HOME/development}"
MODELS_DIR="${OLLAMA_MODELS_DIR:-$DEV_ROOT/models}"
STATE_DIR="${STATE_DIR:-$HOME/.reusable-agents}"
SECRETS_FILE="$STATE_DIR/secrets.env"
OLLAMA_DROPIN="/etc/systemd/system/ollama.service.d/10-models-dir.conf"
IMAGE_DROPIN="$HOME/.config/systemd/user/local-image-gen.service.d/10-model.conf"
GPU_POWER_LIMIT_W="${GPU_POWER_LIMIT_W:-450}"
POWER_CAP_UNIT="/etc/systemd/system/nvidia-power-cap.service"

bold()   { printf "\033[1m%s\033[0m\n" "$*"; }
green()  { printf "  \033[32m✓\033[0m %s\n" "$*"; }
yellow() { printf "  \033[33m!\033[0m %s\n" "$*"; }
red()    { printf "  \033[31m✗\033[0m %s\n" "$*" >&2; }
section(){ echo; bold "── $* ──"; }

if [ "$(id -u)" -eq 0 ]; then
    red "run as the operator user, not root (user units + secrets.env live in \$HOME)"
    exit 1
fi

# ── Desired file contents ───────────────────────────────────────────────────
OLLAMA_DROPIN_CONTENT="[Service]
# Written by reusable-agents/install/configure-local-models.sh — edit there.
#
# ONE resident model. Every agent's local text AND vision call uses
# ${FLEET_LOCAL_MODEL} at num_ctx ${OLLAMA_NUM_CTX} (FLEET_LOCAL_MODEL / OLLAMA_NUM_CTX in
# ~/.reusable-agents/secrets.env; code defaults in framework/core/local_llm.py).
# Ollama reloads a model whenever a request names a different model OR a
# different num_ctx. On 2026-09-23 that churn was 103 cold loads a day, and
# the 10-20 GB load/unload swings are the suspected trigger of the RTX 5090
# Xid-79 'fallen off the bus' drops. So:
#   KEEP_ALIVE=24h        the one model stays resident between agent ticks
#   CONTEXT_LENGTH=65536  = the fleet num_ctx, so a caller that omits
#                         num_ctx still matches the loaded runner
#   NUM_PARALLEL=1        one request at a time -> one 64K KV slot
#   FLASH_ATTENTION=1 +   8-bit KV cache. Measured 2026-09-24: with an f16 KV
#   KV_CACHE_TYPE=q8_0    cache llama-server held 22.5 GB (weights + MTP draft
#                         head + 64K KV + compute buffers — /api/ps under-reports
#                         it as 17.5), which left ~1.6 GB and made every
#                         local-image-gen request (resting ~7 GB, peak ~9 GB)
#                         fail with CUDA OOM. q8_0 halves the KV cache.
#   MAX_LOADED_MODELS=2   headroom for a small auxiliary model (e.g. an
#                         embedding model), NOT a second resident LLM
# Model blobs live on the big data volume with the rest of development/.
Environment=\"OLLAMA_MODELS=${MODELS_DIR}\"
Environment=\"OLLAMA_KEEP_ALIVE=24h\"
Environment=\"OLLAMA_MAX_LOADED_MODELS=2\"
Environment=\"OLLAMA_CONTEXT_LENGTH=${OLLAMA_NUM_CTX}\"
Environment=\"OLLAMA_NUM_PARALLEL=1\"
Environment=\"OLLAMA_FLASH_ATTENTION=1\"
Environment=\"OLLAMA_KV_CACHE_TYPE=q8_0\"
"

IMAGE_DROPIN_CONTENT="[Service]
# Written by reusable-agents/install/configure-local-models.sh — edit there.
# Z-Image-Turbo in low-VRAM mode (services/local-image-gen/README.md): fp8
# transformer weights, Qwen3-4B text encoder on the CPU, 9 steps, guidance 0.
# Measured 8.96 GB peak, so it fits beside the resident ${FLEET_LOCAL_MODEL}.
# Roll back to SDXL-Turbo: set LOCAL_IMAGE_GEN_MODEL=stabilityai/sdxl-turbo and
# delete the other lines (the daemon's sdxl profile supplies fp16 / 4 steps).
Environment=LOCAL_IMAGE_GEN_MODEL=Tongyi-MAI/Z-Image-Turbo
Environment=LOCAL_IMAGE_GEN_DTYPE=bf16
Environment=LOCAL_IMAGE_GEN_FP8_TRANSFORMER=1
Environment=LOCAL_IMAGE_GEN_TEXT_ENCODER_DEVICE=cpu
Environment=LOCAL_IMAGE_GEN_STEPS=9
Environment=LOCAL_IMAGE_GEN_GUIDANCE=0.0
"

OLLAMA_CHANGED=0
IMAGE_CHANGED=0

# ── 1. ollama system drop-in ────────────────────────────────────────────────
section "1. ollama drop-in ($OLLAMA_DROPIN)"
if [ -f "$OLLAMA_DROPIN" ] && [ "$(cat "$OLLAMA_DROPIN")" == "$(printf '%s' "$OLLAMA_DROPIN_CONTENT")" ]; then
    green "already current"
elif [ "$DRY_RUN" -eq 1 ]; then
    yellow "would write (sudo tee):"
    printf '%s' "$OLLAMA_DROPIN_CONTENT" | sed 's/^/      /'
    OLLAMA_CHANGED=1
else
    [ -d "$MODELS_DIR" ] || { red "models dir $MODELS_DIR does not exist"; exit 1; }
    sudo install -d -m 0755 "$(dirname "$OLLAMA_DROPIN")"
    printf '%s' "$OLLAMA_DROPIN_CONTENT" | sudo tee "$OLLAMA_DROPIN" >/dev/null
    green "written"
    OLLAMA_CHANGED=1
fi

# ── 1b. GPU power cap (applied at every boot) ───────────────────────────────
section "1b. GPU power cap ${GPU_POWER_LIMIT_W} W ($POWER_CAP_UNIT)"
POWER_CAP_CONTENT="[Unit]
Description=Cap NVIDIA GPU power limit (MCE / hard-reset crash mitigation)
After=nvidia-persistenced.service
Wants=nvidia-persistenced.service
ConditionPathExists=/usr/bin/nvidia-smi

[Service]
Type=oneshot
RemainAfterExit=yes
# persistence keeps the limit applied when no CUDA client is attached
ExecStart=-/usr/bin/nvidia-smi -pm 1
ExecStart=/usr/bin/nvidia-smi -pl ${GPU_POWER_LIMIT_W}
# restore stock on stop/disable
ExecStop=-/usr/bin/nvidia-smi -pl 575

[Install]
WantedBy=multi-user.target
"
if ! command -v nvidia-smi >/dev/null 2>&1; then
    yellow "no nvidia-smi; skipped"
elif [ -f "$POWER_CAP_UNIT" ] && [ "$(cat "$POWER_CAP_UNIT")" == "$(printf '%s' "$POWER_CAP_CONTENT")" ] \
        && systemctl is-enabled --quiet nvidia-power-cap.service; then
    green "already current ($(nvidia-smi --query-gpu=power.limit --format=csv,noheader))"
elif [ "$DRY_RUN" -eq 1 ]; then
    yellow "would write $POWER_CAP_UNIT and enable --now (sudo)"
else
    printf '%s' "$POWER_CAP_CONTENT" | sudo tee "$POWER_CAP_UNIT" >/dev/null
    sudo systemctl daemon-reload
    sudo systemctl enable nvidia-power-cap.service >/dev/null 2>&1
    sudo systemctl restart nvidia-power-cap.service
    green "enabled; limit now $(nvidia-smi --query-gpu=power.limit --format=csv,noheader)"
fi

# ── 2. secrets.env knobs ────────────────────────────────────────────────────
section "2. fleet knobs in $SECRETS_FILE"
# Append-or-replace KEY='value'. Values are single-quoted (the fleet rule:
# an unquoted ';' truncates on shell source). The file is rewritten in place
# (cat >) so its mode, owner and ACL survive. Nothing from it is printed.
ensure_secret() {
    local key="$1" val="$2"
    local line="${key}='${val}'"
    if [ -f "$SECRETS_FILE" ] && grep -qxF "$line" "$SECRETS_FILE"; then
        green "$key already set"
        return
    fi
    if [ "$DRY_RUN" -eq 1 ]; then
        yellow "would set $line"
        return
    fi
    if [ ! -f "$SECRETS_FILE" ]; then
        (umask 077; mkdir -p "$STATE_DIR"; : > "$SECRETS_FILE")
    fi
    if grep -qE "^(export +)?${key}=" "$SECRETS_FILE"; then
        local tmp
        tmp="$(mktemp)"
        awk -v k="$key" -v l="$line" '
            $0 ~ "^(export +)?" k "=" { if (!done) { print l; done = 1 }; next }
            { print }' "$SECRETS_FILE" > "$tmp"
        cat "$tmp" > "$SECRETS_FILE"
        rm -f "$tmp"
        green "$key replaced"
    else
        if [ -s "$SECRETS_FILE" ] && [ -n "$(tail -c1 "$SECRETS_FILE")" ]; then
            printf '\n' >> "$SECRETS_FILE"
        fi
        printf '%s\n' "$line" >> "$SECRETS_FILE"
        green "$key appended"
    fi
}
ensure_secret FLEET_LOCAL_MODEL "$FLEET_LOCAL_MODEL"
ensure_secret OLLAMA_NUM_CTX "$OLLAMA_NUM_CTX"

# ── 3. local-image-gen user drop-in ─────────────────────────────────────────
section "3. local-image-gen drop-in ($IMAGE_DROPIN)"
if [ -f "$IMAGE_DROPIN" ] && [ "$(cat "$IMAGE_DROPIN")" == "$(printf '%s' "$IMAGE_DROPIN_CONTENT")" ]; then
    green "already current"
elif [ "$DRY_RUN" -eq 1 ]; then
    yellow "would write:"
    printf '%s' "$IMAGE_DROPIN_CONTENT" | sed 's/^/      /'
    IMAGE_CHANGED=1
else
    mkdir -p "$(dirname "$IMAGE_DROPIN")"
    printf '%s' "$IMAGE_DROPIN_CONTENT" > "$IMAGE_DROPIN"
    green "written"
    IMAGE_CHANGED=1
fi

[ "$FORCE_RESTART" -eq 1 ] && { OLLAMA_CHANGED=1; IMAGE_CHANGED=1; }

if [ "$DRY_RUN" -eq 1 ]; then
    section "4-6. would run"
    [ "$OLLAMA_CHANGED" -eq 1 ] && echo "      sudo systemctl daemon-reload && sudo systemctl restart ollama"
    echo "      ollama pull $FLEET_LOCAL_MODEL"
    echo "      warm: POST $OLLAMA_URL/api/generate {model: $FLEET_LOCAL_MODEL, options: {num_ctx: $OLLAMA_NUM_CTX}}"
    [ "$IMAGE_CHANGED" -eq 1 ] && echo "      systemctl --user daemon-reload && systemctl --user restart local-image-gen"
    echo "      verify: ollama ps; curl $IMAGE_GEN_URL/healthz"
    exit 0
fi

wait_for() {  # URL SECONDS
    local url="$1" secs="$2" i
    for ((i = 0; i < secs; i += 2)); do
        curl -sf --max-time 3 "$url" >/dev/null 2>&1 && return 0
        sleep 2
    done
    return 1
}

# ── 4-5. restart ollama, pull, warm ─────────────────────────────────────────
section "4. ollama: restart + pull + warm"
if [ "$OLLAMA_CHANGED" -eq 1 ]; then
    sudo systemctl daemon-reload
    sudo systemctl restart ollama
    green "ollama restarted (all previously loaded models unloaded)"
fi
wait_for "$OLLAMA_URL/api/tags" 60 || { red "ollama not answering on $OLLAMA_URL"; exit 1; }
ollama pull "$FLEET_LOCAL_MODEL"
green "$FLEET_LOCAL_MODEL pulled"
# Warm at the fleet num_ctx: a warm-up with any other num_ctx would itself
# force a reload on the first real request.
if curl -sf --max-time 300 "$OLLAMA_URL/api/generate" \
        -d "{\"model\":\"$FLEET_LOCAL_MODEL\",\"prompt\":\"\",\"stream\":false,\"think\":false,\"options\":{\"num_ctx\":$OLLAMA_NUM_CTX}}" \
        >/dev/null; then
    green "$FLEET_LOCAL_MODEL resident at num_ctx $OLLAMA_NUM_CTX"
else
    yellow "warm-up call failed; the first agent call will load it"
fi

section "5. local-image-gen: restart"
systemctl --user daemon-reload
if [ "$IMAGE_CHANGED" -eq 1 ]; then
    systemctl --user restart local-image-gen
    green "local-image-gen restarted; waiting for the model to load (up to 10 min)"
    wait_for "$IMAGE_GEN_URL/healthz" 120 || true
    for _ in $(seq 1 60); do
        curl -sf --max-time 3 "$IMAGE_GEN_URL/healthz" 2>/dev/null | grep -q '"model_loaded":true' && break
        sleep 10
    done
else
    green "config unchanged; not restarted (use --restart to force)"
fi

# ── 6. verification ─────────────────────────────────────────────────────────
section "6. verification"
echo "  ollama ps:"
ollama ps | sed 's/^/      /'
others="$(ollama ps | awk 'NR > 1 && $1 != "" {print $1}' | grep -vxF "$FLEET_LOCAL_MODEL" || true)"
if [ -n "$others" ]; then
    yellow "other models resident (a caller is not on the fleet model):"
    printf '%s\n' "$others" | sed 's/^/      /'
else
    green "only $FLEET_LOCAL_MODEL resident"
fi
echo "  local-image-gen /healthz:"
health="$(curl -s --max-time 5 "$IMAGE_GEN_URL/healthz" || true)"
echo "      ${health:-<no answer>}"
if printf '%s' "$health" | grep -q '"model":"Tongyi-MAI/Z-Image-Turbo"' \
        && printf '%s' "$health" | grep -q '"model_loaded":true'; then
    green "Z-Image-Turbo loaded"
else
    yellow "image daemon not ready yet: journalctl --user -u local-image-gen -f"
fi
for key in FLEET_LOCAL_MODEL OLLAMA_NUM_CTX; do
    grep -qE "^${key}=" "$SECRETS_FILE" && green "$key present in secrets.env" \
        || red "$key missing from secrets.env"
done
echo
echo "  Agent units read secrets.env at each start, so the next tick picks up"
echo "  the knobs. Long-running services (host-worker, drainer) keep their old"
echo "  environment until restarted; the code defaults are the same values."
