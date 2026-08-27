# local-image-gen

A locally-hosted text-to-image daemon for AislePrompt + SpecPicks.
Replaces every paid image-generation path (Azure OpenAI gpt-image-1 /
DALL-E, fal.ai, Replicate) with a free SDXL-Turbo model on the dev
box's RTX 5090.

Bench: **~0.6 s per 1024×1024 image, ~100 RPM, ~6,000 images/hour,
~$0.0003/image (electricity).** Replaces calls that previously cost
$0.04/image at Azure pricing — a **140× cost reduction** at higher
throughput than the Azure 8-RPM quota cap.

## Endpoints

| Path | Auth | Body | Response |
|---|---|---|---|
| `POST /generate` | Bearer | `{prompt, width?, height?, steps?, seed?, guidance_scale?, negative_prompt?}` | `image/png` bytes |
| `GET /healthz` | — | — | `{status, model, gpu, vram_mb_used}` |
| `GET /metrics` | — | — | `{requests_total, errors_total, sec_p50, sec_p99}` |

Default port: **7861** (localhost-only). Default model:
`stabilityai/sdxl-turbo`. Override via env vars (see below).

## Env vars

| Var | Default | Purpose |
|---|---|---|
| `LOCAL_IMAGE_GEN_MODEL` | `stabilityai/sdxl-turbo` | Any HuggingFace text-to-image checkpoint. Use FLUX-schnell once you've accepted the BFL license + set HF_TOKEN. |
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
  body: JSON.stringify({ prompt, width: 1024, height: 1024, steps: 4 }),
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
    json={"prompt": "...", "width": 1024, "height": 1024, "steps": 4},
    timeout=30,
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
# First run downloads ~7 GB of SDXL-Turbo weights into ~/.cache/huggingface
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

SDXL-Turbo holds ~10.5 GB resident. The RTX 5090 (32 GB) has room for
Ollama models alongside, but be aware that loading a giant Ollama
model (e.g. Kimi-Dev-72B at 38 GB) will force-evict the diffusion
weights. If you regularly need both at once:

- Drop to `stabilityai/stable-diffusion-2-1` (~4 GB) for SDXL → SD2.1.
- Or run the daemon's `LOCAL_IMAGE_GEN_MODEL=stabilityai/sdxl-turbo`
  on the 5090 and keep Ollama models that exceed remaining VRAM on
  a sibling box.

## Quality tuning

SDXL-Turbo at 4 steps is the speed/quality sweet spot for recipe
thumbnails. To trade speed for quality:

- `steps=6` → ~0.9 s, slightly cleaner detail
- `steps=8` → ~1.2 s, diminishing returns past 8 (Turbo is trained
  for 1-4 steps; 8 is the practical ceiling)

For high-end work (article hero, news hero), switch the model env to
`black-forest-labs/FLUX.1-schnell` (needs HF auth + license acceptance)
or `black-forest-labs/FLUX.1-dev` (28 steps, ~4 s/image, best quality).

## VRAM budget — this daemon shares the GPU

The 5090 is also where ollama serves the agent fleet's local models, and SDXL
loses that fight silently. Two failure modes, both observed 2026-08-27:

* Under pressure the diffusers pipeline ends up with fp16 activations meeting
  an fp32 bias and every request 500s with "Input type (c10::Half) and bias
  type (float) should be the same" — permanently. The generate handler now
  self-heals by rebuilding the pipeline (`pipeline_reloads_total` in
  /metrics), but the reload needs free VRAM to succeed.
* If the GPU is genuinely full the reload cannot complete and /healthz sits
  at `"status":"loading"` indefinitely.

So: **before routing an agent to a local model, check the model's resident
size, not just its quality.** Routing the eBay sync agent to `qwen3:14b`
looked right on quality (5/5 extractions) but the model sits at **14.5 GB**
resident, which evicted SDXL and stalled an 8,000-image backfill. `qwen3:8b`
scores the same 5/5 at **5.3 GB** and coexists with SDXL's ~7 GB.

Check with `curl -s localhost:11434/api/ps` — that reports `size_vram`, which
is what matters, not the on-disk size in `/api/tags`.
