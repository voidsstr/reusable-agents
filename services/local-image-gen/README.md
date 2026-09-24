# local-image-gen

A locally-hosted text-to-image daemon for AislePrompt + SpecPicks.
Replaces every paid image-generation path (Azure OpenAI gpt-image-1 /
DALL-E, fal.ai, Replicate) with a free local model on the fleet host's
RTX 5090.

Model (since 2026-09-23): **Tongyi-MAI/Z-Image-Turbo** (Apache-2.0). In a
blind comparison on real refiller prompts, judges preferred it to
SDXL-Turbo 9/12. It runs in a low-VRAM mode so it fits beside the fleet's
one resident Ollama model (`qwen3.8:27b`, ~17.5 GB at 64K context):

- transformer weights stored in fp8, computed in bf16 (diffusers layerwise casting)
- the Qwen3-4B text encoder on the **CPU**; prompts are encoded there and
  only the embeddings go to the GPU
- 9 steps (= 8 DiT forwards), guidance 0.0, 1024×1024

Measured: **8.96 GB peak VRAM, ~8–16 s/image** (median 15.6 s in the
2026-09-23 bench), or roughly 230–450 images/hour. SDXL-Turbo took
~0.6 s. Each image costs electricity only, where the Azure path cost
$0.04/image.

## Endpoints

| Path | Auth | Body | Response |
|---|---|---|---|
| `POST /generate` | Bearer | `{prompt, width?, height?, steps?, seed?, guidance_scale?, negative_prompt?}` | `image/png` bytes |
| `GET /healthz` | — | — | `{status, model, model_loaded, gpu, vram_mb_used, dtype, fp8_transformer, text_encoder_device, default_steps, default_guidance}` |
| `GET /metrics` | — | — | `{requests_total, errors_total, sec_p50, sec_p99}` |

Default port: **7861** (localhost-only). **The daemon owns the model's
defaults**: omit `steps` and `guidance_scale`. A `steps` value below the
model's useful minimum (e.g. an SDXL-era `steps: 4` sent to Z-Image, whose
minimum is 8) is replaced by the model default and logged once. Width and
height are snapped down to a multiple of 16.

## Env vars

| Var | Default | Purpose |
|---|---|---|
| `LOCAL_IMAGE_GEN_MODEL` | `Tongyi-MAI/Z-Image-Turbo` | Any diffusers text-to-image checkpoint, loaded with `DiffusionPipeline`. `stabilityai/sdxl-turbo` still works (fp16, 4 steps). |
| `LOCAL_IMAGE_GEN_DTYPE` | `bf16` (`fp16` for sdxl-turbo) | Compute dtype: `bf16`, `fp16` or `fp32`. |
| `LOCAL_IMAGE_GEN_FP8_TRANSFORMER` | `1` for Z-Image, else `0` | Store the denoiser's weights in fp8 (about half the VRAM). |
| `LOCAL_IMAGE_GEN_TEXT_ENCODER_DEVICE` | `cpu` for Z-Image, else `cuda` | `cpu` works only for Z-Image; other pipelines log a warning and use cuda. |
| `LOCAL_IMAGE_GEN_STEPS` | `9` for Z-Image, `4` for sdxl-turbo | Default steps when a request omits them. |
| `LOCAL_IMAGE_GEN_GUIDANCE` | `0.0` | Default guidance. Z-Image turns on CFG (twice the compute) for any value above 0. |
| `LOCAL_IMAGE_GEN_HOST` | `127.0.0.1` | Bind address. Leave at localhost; daemon is not auth-strong enough for public exposure. |
| `LOCAL_IMAGE_GEN_PORT` | `7861` | Listen port. |
| `LOCAL_IMAGE_GEN_TOKEN` | `dev-local-image-gen-token` | Bearer token clients send. Override for prod. |
| `HF_TOKEN` | (unset) | Optional; needed for gated models like FLUX-schnell. |

## Usage from a client

### TypeScript (refiller pattern)

```typescript
const res = await fetch(`${LOCAL_IMAGE_GEN_URL}/generate`, {
  method: 'POST',
  headers: {
    'Authorization': `Bearer ${LOCAL_IMAGE_GEN_TOKEN}`,
    'Content-Type': 'application/json',
  },
  body: JSON.stringify({ prompt, width: 1024, height: 1024 }),  // daemon picks steps
});
if (!res.ok) throw new Error(await res.text());
const bytes = Buffer.from(await res.arrayBuffer());
```

### Python (any agent)

```python
import requests
r = requests.post(
    f"{LOCAL_IMAGE_GEN_URL}/generate",
    headers={"Authorization": f"Bearer {TOKEN}"},
    json={"prompt": "...", "width": 1024, "height": 1024},  # daemon picks steps
    timeout=60,
)
r.raise_for_status()
png_bytes = r.content
```

## Install

Once. The systemd unit handles startup thereafter.

```bash
cd /home/voidsstr/development/reusable-agents/services/local-image-gen
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
# First run downloads ~31 GB of Z-Image-Turbo weights into ~/.cache/huggingface
.venv/bin/python server.py
```

## Run as a service

The systemd unit at
`/home/voidsstr/.config/systemd/user/local-image-gen.service` starts
the daemon on login + restarts on failure. Manage with:

```
systemctl --user start local-image-gen
systemctl --user enable local-image-gen   # auto-start on login
systemctl --user status local-image-gen
journalctl --user -u local-image-gen -f
```

## Hard rule: NO paid image gen elsewhere in the codebase

Anywhere a service or agent needs an image generation:

1. POST `localhost:7861/generate`.
2. If that fails, **fail the operation** — do NOT fall back to a paid
   provider. The daemon should be running; if it's not, that's an
   ops bug worth surfacing as an error rather than silently spending.
3. If you need a different model for a particular use case, run a
   second instance on a different port with `LOCAL_IMAGE_GEN_MODEL=...`
   rather than reaching for a paid API.

See the project root `CLAUDE.md` "Image generation — local only"
section for the full policy.

## VRAM coexistence with Ollama

The host runs one resident Ollama model (`qwen3.8:27b` at `num_ctx` 65536,
~17.5 GB) next to this daemon (8.96 GB peak in the low-VRAM mode), which
leaves headroom on the 32 GB card. Keep it that way: do not add a second
resident LLM, and do not turn off `LOCAL_IMAGE_GEN_FP8_TRANSFORMER` or move
the text encoder to cuda without re-measuring. Both change the budget by
several GB.

## Quality tuning

Z-Image-Turbo is distilled for 8 DiT forwards (`steps=9`). The daemon
treats fewer than 8 steps as a mistake and uses the default instead.

To go back to SDXL-Turbo (~0.6 s/image, ~7 GB, lower quality), set
`LOCAL_IMAGE_GEN_MODEL=stabilityai/sdxl-turbo` in the unit's drop-in
(`install/configure-local-models.sh` writes it) and restart the service.

## VRAM budget — this daemon shares the GPU

The 5090 is also where ollama serves the agent fleet's local model, and the
diffusion pipeline loses that fight silently. Two failure modes, both observed
2026-08-27 (with SDXL-Turbo):

* Under pressure the diffusers pipeline ends up with half-precision
  activations meeting an fp32 bias and every request 500s with "Input type
  (c10::Half) and bias type (float) should be the same" — permanently. The generate handler now
  self-heals by rebuilding the pipeline (`pipeline_reloads_total` in
  /metrics), but the reload needs free VRAM to succeed.
* If the GPU is genuinely full the reload cannot complete and /healthz sits
  at `"status":"loading"` indefinitely.

So: **every local LLM call uses the one fleet model** (`FLEET_LOCAL_MODEL`,
see `framework/core/local_llm.py`). In 2026-08/09, agents on different models
(`qwen3:8b`, `qwen3:14b`, `minicpm-v4.5`) evicted each other and this daemon.
One run evicted SDXL and stalled an 8,000-image backfill.

Check with `curl -s localhost:11434/api/ps` — that reports `size_vram`, which
is what matters, not the on-disk size in `/api/tags`.
