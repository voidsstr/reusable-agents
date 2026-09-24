"""
local-image-gen — text-to-image daemon for AislePrompt + SpecPicks.

Replaces all paid image generation (Azure OpenAI gpt-image-1, DALL-E,
fal.ai, etc.) with a free, locally-hosted model on the RTX 5090.

Default model: Tongyi-MAI/Z-Image-Turbo (Apache-2.0; blind judges preferred
it over SDXL-Turbo 9/12 on real refiller prompts, 2026-09-23), run in a
low-VRAM mode so it coexists with the fleet's resident Ollama model:
  * transformer weights stored in fp8, computed in bf16 (layerwise casting)
  * the Qwen3-4B text encoder on CPU; prompts are encoded there and only the
    embeddings move to the GPU
  * 9 steps (= 8 DiT forwards), guidance 0.0, 1024x1024
Measured 8.96 GB peak VRAM, ~8-16 s/image. stabilityai/sdxl-turbo still
works with LOCAL_IMAGE_GEN_MODEL=stabilityai/sdxl-turbo (fp16, 4 steps).

Architecture:
  POST /generate     {prompt, width=1024, height=1024, steps?, seed?,
                      guidance_scale?, negative_prompt?}
                     → PNG bytes (image/png response)
  GET  /healthz      → {status, model, model_loaded, gpu, vram_mb_used, ...}
  GET  /metrics      → {requests_total, errors_total, sec_p50, sec_p99}

The daemon owns model-appropriate defaults: omit `steps` / `guidance_scale`
and it uses the model's values. A `steps` below the model's useful minimum
(e.g. an SDXL-era client sending steps=4 to Z-Image) is replaced by the
model default and logged.

Env knobs (defaults come from the model's profile below):
  LOCAL_IMAGE_GEN_MODEL                 Tongyi-MAI/Z-Image-Turbo
  LOCAL_IMAGE_GEN_DTYPE                 bf16 (fp16 for sdxl-turbo)
  LOCAL_IMAGE_GEN_FP8_TRANSFORMER       1 for Z-Image, else 0
  LOCAL_IMAGE_GEN_TEXT_ENCODER_DEVICE   cpu for Z-Image, else cuda
  LOCAL_IMAGE_GEN_STEPS                 9 for Z-Image, 4 for sdxl-turbo
  LOCAL_IMAGE_GEN_GUIDANCE              0.0

Auth:
  Authorization: Bearer <LOCAL_IMAGE_GEN_TOKEN>
  Token defaults to a fixed development value; production sets via
  env. Bound to 127.0.0.1 by default — defence in depth, not the
  primary control.

Concurrency:
  Generation is serialized through an asyncio.Lock — PyTorch is not
  re-entrant on the same pipeline.

Operational notes:
  - The venv runs torch 2.10.0; stay below 2.11, which turns the
    kernel-driver-version-mismatch warning into a fatal NVML assert.
  - Startup loads the model and runs one warmup image before serving.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Optional

import torch
from diffusers import DiffusionPipeline
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

# ────────────────────────────────────────────────────────────────────
# Config
# ────────────────────────────────────────────────────────────────────
MODEL_ID = os.environ.get("LOCAL_IMAGE_GEN_MODEL", "Tongyi-MAI/Z-Image-Turbo")
HOST = os.environ.get("LOCAL_IMAGE_GEN_HOST", "127.0.0.1")
PORT = int(os.environ.get("LOCAL_IMAGE_GEN_PORT", "7861"))
TOKEN = os.environ.get("LOCAL_IMAGE_GEN_TOKEN", "dev-local-image-gen-token")
DEFAULT_DIM = 1024
MAX_DIM = 1536
MIN_DIM = 256

# Per-model defaults, matched by substring of the model id. min_steps is the
# lowest step count worth running; below it the model default is used.
_PROFILES = {
    "z-image": {"steps": 9, "min_steps": 8, "dtype": "bf16", "variant": None,
                "fp8": True, "te_device": "cpu"},
    "sdxl-turbo": {"steps": 4, "min_steps": 1, "dtype": "fp16", "variant": "fp16",
                   "fp8": False, "te_device": "cuda"},
}
_GENERIC = {"steps": 20, "min_steps": 1, "dtype": "bf16", "variant": None,
            "fp8": False, "te_device": "cuda"}
PROFILE = next((p for k, p in _PROFILES.items() if k in MODEL_ID.lower()), _GENERIC)

_DTYPES = {"bf16": torch.bfloat16, "bfloat16": torch.bfloat16,
           "fp16": torch.float16, "float16": torch.float16,
           "fp32": torch.float32, "float32": torch.float32}


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or not v.strip():
        return default
    return v.strip().lower() not in ("0", "false", "no", "off")


DTYPE_NAME = os.environ.get("LOCAL_IMAGE_GEN_DTYPE", PROFILE["dtype"]).strip().lower()
DTYPE = _DTYPES[DTYPE_NAME]
FP8_TRANSFORMER = _env_bool("LOCAL_IMAGE_GEN_FP8_TRANSFORMER", PROFILE["fp8"])
TEXT_ENCODER_DEVICE = os.environ.get(
    "LOCAL_IMAGE_GEN_TEXT_ENCODER_DEVICE", PROFILE["te_device"]).strip().lower()
DEFAULT_STEPS = int(os.environ.get("LOCAL_IMAGE_GEN_STEPS", PROFILE["steps"]))
MIN_STEPS = min(PROFILE["min_steps"], DEFAULT_STEPS)
DEFAULT_GUIDANCE = float(os.environ.get("LOCAL_IMAGE_GEN_GUIDANCE", "0.0"))

# ────────────────────────────────────────────────────────────────────
# Globals (loaded once on app startup)
# ────────────────────────────────────────────────────────────────────
pipe = None  # type: ignore[assignment]
te_on_cpu = False  # True once the text encoder actually lives on the CPU
gen_lock = asyncio.Lock()
metrics = {
    "requests_total": 0,
    "errors_total": 0,
    "secs": [],  # rolling window of last 256 latencies (seconds)
}
_steps_adjusted_logged: set[int] = set()

logger = logging.getLogger("local-image-gen")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def _load_pipeline():
    """Build the pipeline per the env/profile knobs. Returns (pipe, te_on_cpu)."""
    kw = {"torch_dtype": DTYPE}
    if PROFILE["variant"] and DTYPE == torch.float16:
        kw["variant"] = PROFILE["variant"]
    # DiffusionPipeline, not AutoPipelineForText2Image: it resolves any
    # model's own pipeline class from model_index.json.
    p = DiffusionPipeline.from_pretrained(MODEL_ID, **kw)

    denoiser = getattr(p, "transformer", None) or getattr(p, "unet", None)
    if FP8_TRANSFORMER and denoiser is not None:
        # Weights stored in fp8, upcast per layer for compute: roughly halves
        # the denoiser's VRAM so the daemon fits beside the resident LLM.
        denoiser.enable_layerwise_casting(
            storage_dtype=torch.float8_e4m3fn, compute_dtype=DTYPE)

    # CPU text encoding is only wired for Z-Image's single-encoder
    # encode_prompt(); other pipelines keep every component on the GPU.
    cpu_te = TEXT_ENCODER_DEVICE == "cpu" and type(p).__name__.startswith("ZImage")
    if TEXT_ENCODER_DEVICE == "cpu" and not cpu_te:
        logger.warning("text encoder on CPU is not supported for %s; using cuda",
                       type(p).__name__)
    if cpu_te:
        for name, comp in p.components.items():
            if isinstance(comp, torch.nn.Module):
                comp.to("cpu" if name.startswith("text_encoder") else "cuda")
    else:
        p.to("cuda")
    # Tiled VAE decode: the 1024x1024 decode is the daemon's largest single
    # allocation, and the daemon shares the card with the resident LLM
    # (~20 GB). Without tiling the decode OOMed with ~1.6 GB free.
    vae = getattr(p, "vae", None)
    if vae is not None and hasattr(vae, "enable_tiling"):
        try:
            vae.enable_tiling()
        except Exception as e:  # noqa: BLE001 — tiling is an optimisation only
            logger.warning("vae tiling unavailable for %s: %s", type(p).__name__, e)
    return p, cpu_te


def _reload_pipeline() -> None:
    """Rebuild the diffusers pipeline from scratch, freeing the broken one."""
    global pipe, te_on_cpu
    import gc
    old = pipe
    pipe = None
    del old
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    pipe, te_on_cpu = _load_pipeline()


def _to_cuda(embeds):
    if isinstance(embeds, (list, tuple)):
        return [e.to("cuda") for e in embeds]
    return embeds.to("cuda")


def _render_png(prompt: str, negative_prompt: Optional[str], steps: int,
                guidance: float, height: int, width: int,
                generator: Optional[torch.Generator]) -> bytes:
    kw = {"num_inference_steps": steps, "guidance_scale": guidance,
          "height": height, "width": width, "generator": generator}
    # no_grad, not inference_mode: fp8 layerwise casting re-casts weights in
    # forward hooks, and inference tensors must not leak into module params.
    # no_grad is also what the measured Z-Image bench ran under.
    with torch.no_grad():
        if te_on_cpu:
            # Z-Image enables CFG for any guidance > 0, and then needs
            # negative embeddings as well.
            cfg = guidance > 0
            pe, npe = pipe.encode_prompt(
                prompt=prompt, device="cpu", do_classifier_free_guidance=cfg,
                negative_prompt=negative_prompt if cfg else None)
            kw["prompt_embeds"] = _to_cuda(pe)
            if cfg:
                kw["negative_prompt_embeds"] = _to_cuda(npe)
        else:
            kw["prompt"] = prompt
            if negative_prompt:
                kw["negative_prompt"] = negative_prompt
        img = pipe(**kw).images[0]
    # Hand the activation memory back to the driver after every image so the
    # daemon rests at its weights (~6 GB), not its peak, between requests.
    torch.cuda.empty_cache()
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load the model on startup; cleanup on shutdown."""
    global pipe, te_on_cpu
    logger.info(f"loading model: {MODEL_ID} dtype={DTYPE_NAME} fp8={FP8_TRANSFORMER} "
                f"text_encoder={TEXT_ENCODER_DEVICE} steps={DEFAULT_STEPS} "
                f"guidance={DEFAULT_GUIDANCE}")
    t = time.time()
    pipe, te_on_cpu = _load_pipeline()
    # Warmup so the first user request is fast.
    logger.info("warming up...")
    _render_png("professional food photography, photorealistic", None,
                DEFAULT_STEPS, DEFAULT_GUIDANCE, DEFAULT_DIM, DEFAULT_DIM, None)
    torch.cuda.empty_cache()
    logger.info(f"model ready in {time.time()-t:.1f}s; peak vram {torch.cuda.max_memory_allocated()/1024**3:.1f} GB")
    yield
    # Shutdown
    logger.info("shutting down")


app = FastAPI(title="local-image-gen", lifespan=lifespan)
bearer = HTTPBearer(auto_error=False)


def check_auth(creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer)):
    if creds is None or creds.credentials != TOKEN:
        raise HTTPException(status_code=401, detail="bad token")


class GenerateReq(BaseModel):
    """Body for POST /generate. Omit steps/guidance_scale to get the
    model's defaults."""
    prompt: str = Field(..., min_length=1, max_length=2000)
    negative_prompt: Optional[str] = Field(None, max_length=2000)
    width: int = Field(DEFAULT_DIM, ge=MIN_DIM, le=MAX_DIM)
    height: int = Field(DEFAULT_DIM, ge=MIN_DIM, le=MAX_DIM)
    steps: Optional[int] = Field(None, ge=1, le=50)
    guidance_scale: Optional[float] = Field(None, ge=0.0, le=10.0)
    seed: Optional[int] = None


def _resolve_steps(requested: Optional[int]) -> int:
    if requested is None:
        return DEFAULT_STEPS
    if requested < MIN_STEPS:
        if requested not in _steps_adjusted_logged:
            _steps_adjusted_logged.add(requested)
            logger.info("steps=%d is below %s's useful minimum (%d); using the "
                        "model default %d (logged once per value)",
                        requested, MODEL_ID, MIN_STEPS, DEFAULT_STEPS)
        return DEFAULT_STEPS
    return requested


@app.get("/healthz")
async def healthz():
    used = (torch.cuda.memory_allocated() / 1024**2) if torch.cuda.is_available() else 0
    return {
        "status": "ok" if pipe is not None else "loading",
        "model": MODEL_ID,
        "model_loaded": pipe is not None,
        "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
        "vram_mb_used": round(used, 1),
        "dtype": DTYPE_NAME,
        "fp8_transformer": FP8_TRANSFORMER,
        "text_encoder_device": "cpu" if te_on_cpu else "cuda",
        "default_steps": DEFAULT_STEPS,
        "default_guidance": DEFAULT_GUIDANCE,
    }


@app.get("/metrics")
async def metrics_endpoint():
    secs = sorted(metrics["secs"])
    p50 = secs[len(secs) // 2] if secs else None
    p99 = secs[int(len(secs) * 0.99)] if secs else None
    return {
        "requests_total": metrics["requests_total"],
        "errors_total": metrics["errors_total"],
        "sec_p50": round(p50, 3) if p50 else None,
        "sec_p99": round(p99, 3) if p99 else None,
        "window_size": len(secs),
    }


@app.post("/generate", dependencies=[Depends(check_auth)])
async def generate(req: GenerateReq):
    if pipe is None:
        raise HTTPException(status_code=503, detail="model loading")
    metrics["requests_total"] += 1
    t0 = time.time()
    steps = _resolve_steps(req.steps)
    guidance = DEFAULT_GUIDANCE if req.guidance_scale is None else req.guidance_scale
    # Z-Image needs multiples of 16 (SDXL of 8); snap down rather than 500.
    height = max(MIN_DIM, req.height // 16 * 16)
    width = max(MIN_DIM, req.width // 16 * 16)
    try:
        gen = None
        if req.seed is not None:
            gen = torch.Generator(device="cuda").manual_seed(int(req.seed))
        async with gen_lock:
            # Run on a worker thread so the event loop stays responsive.
            png = await asyncio.to_thread(
                _render_png, req.prompt, req.negative_prompt, steps, guidance,
                height, width, gen)
            torch.cuda.empty_cache()
        dt = time.time() - t0
        metrics["secs"].append(dt)
        if len(metrics["secs"]) > 256:
            metrics["secs"] = metrics["secs"][-256:]
        return Response(
            content=png,
            media_type="image/png",
            headers={
                "X-Generation-Sec": f"{dt:.3f}",
                "X-Model": MODEL_ID,
                "X-Steps": str(steps),
            },
        )
    except Exception as e:
        metrics["errors_total"] += 1
        logger.exception("generate failed")
        # Self-heal the one fault that does not recover on its own.
        #
        # Under GPU pressure the diffusers pipeline can end up with half-
        # precision activations meeting an fp32 bias and every subsequent
        # request dies with "Input type (c10::Half) and bias type (float)
        # should be the same" -- permanently. /healthz keeps reporting ok
        # because the model object is still loaded, so nothing notices; on
        # 2026-08-27 this silently stopped an 8,000-image backfill twice, and
        # only a manual `systemctl restart` cleared it. Rebuild the pipeline
        # in place instead of waiting for a human.
        if "bias type" in str(e) or "c10::Half" in str(e) or "c10::BFloat16" in str(e):
            logger.error("dtype fault detected — reloading pipeline in place")
            try:
                _reload_pipeline()
                metrics["pipeline_reloads_total"] = \
                    metrics.get("pipeline_reloads_total", 0) + 1
                logger.info("pipeline reloaded; next request should succeed")
            except Exception:
                logger.exception("pipeline reload FAILED — restart required")
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host=HOST,
        port=PORT,
        log_level="info",
        access_log=False,
    )
