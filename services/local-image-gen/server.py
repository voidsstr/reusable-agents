"""
local-image-gen — text-to-image daemon for AislePrompt + SpecPicks.

Replaces all paid image generation (Azure OpenAI gpt-image-1, DALL-E,
fal.ai, etc.) with a free, fast, locally-hosted SDXL-Turbo model on
the RTX 5090. Bench shows ~0.6s/image (1024×1024, 4 inference steps),
~6,000 images/hour, $0 marginal cost.

Architecture:
  POST /generate     {prompt, width=1024, height=1024, steps=4, seed?}
                     → PNG bytes (image/png response)
  GET  /healthz      → {status, model_loaded, gpu, vram_mb_used}
  GET  /metrics      → {requests_total, errors_total, sec_p50, sec_p99}

Auth:
  Authorization: Bearer <LOCAL_IMAGE_GEN_TOKEN>
  Token defaults to a fixed development value; production sets via
  env. Bound to 127.0.0.1 by default — defence in depth, not the
  primary control.

VRAM coexistence:
  SDXL-Turbo needs ~10.5 GB at fp16. The 5090 has 32 GB total; Ollama
  models share the rest. The model stays resident (cold start is ~30s),
  but each request runs torch.cuda.empty_cache() after to release the
  intermediate VAE buffers.

Concurrency:
  Generation is serialized through an asyncio.Lock — PyTorch is not
  re-entrant on the same pipeline. Throughput is one request at a
  time; with 0.6s/image that's still ~100 RPM ceiling per card.

Operational notes:
  - Reload model with `kill -HUP <pid>` — handled by uvicorn workers.
  - Pin torch to 2.8.x so the kernel-driver-version-mismatch warning
    stays a warning (torch ≥2.11 turns it into a fatal NVML assert).
  - On startup the model takes ~30s to load + 1s warmup. The first
    real request returns in standard time.
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
from diffusers import AutoPipelineForText2Image
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import Response, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

# ────────────────────────────────────────────────────────────────────
# Config
# ────────────────────────────────────────────────────────────────────
MODEL_ID = os.environ.get("LOCAL_IMAGE_GEN_MODEL", "stabilityai/sdxl-turbo")
HOST = os.environ.get("LOCAL_IMAGE_GEN_HOST", "127.0.0.1")
PORT = int(os.environ.get("LOCAL_IMAGE_GEN_PORT", "7861"))
TOKEN = os.environ.get("LOCAL_IMAGE_GEN_TOKEN", "dev-local-image-gen-token")
DTYPE = torch.float16
DEFAULT_STEPS = 4
DEFAULT_GUIDANCE = 0.0  # SDXL-Turbo recommends 0.0
DEFAULT_DIM = 1024
MAX_DIM = 1536
MIN_DIM = 256

# ────────────────────────────────────────────────────────────────────
# Globals (loaded once on app startup)
# ────────────────────────────────────────────────────────────────────
pipe = None  # type: ignore[assignment]
gen_lock = asyncio.Lock()
metrics = {
    "requests_total": 0,
    "errors_total": 0,
    "secs": [],  # rolling window of last 256 latencies (seconds)
}

logger = logging.getLogger("local-image-gen")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def _reload_pipeline() -> None:
    """Rebuild the diffusers pipeline from scratch, freeing the broken one.

    Mirrors the load in `lifespan`. Kept as its own function so the startup
    path and the self-heal path cannot drift apart.
    """
    global pipe
    import gc
    old = pipe
    pipe = None
    del old
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    pipe = AutoPipelineForText2Image.from_pretrained(
        MODEL_ID,
        torch_dtype=DTYPE,
        variant="fp16",
    ).to("cuda")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load the model on startup; cleanup on shutdown."""
    global pipe
    logger.info(f"loading model: {MODEL_ID}")
    t = time.time()
    pipe = AutoPipelineForText2Image.from_pretrained(
        MODEL_ID,
        torch_dtype=DTYPE,
        variant="fp16",
    ).to("cuda")
    # Warmup compile so the first user request is fast.
    logger.info("warming up...")
    with torch.inference_mode():
        _ = pipe(
            "professional food photography, photorealistic",
            num_inference_steps=DEFAULT_STEPS,
            guidance_scale=DEFAULT_GUIDANCE,
            height=DEFAULT_DIM,
            width=DEFAULT_DIM,
        ).images[0]
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
    """Body for POST /generate."""
    prompt: str = Field(..., min_length=1, max_length=2000)
    negative_prompt: Optional[str] = Field(None, max_length=2000)
    width: int = Field(DEFAULT_DIM, ge=MIN_DIM, le=MAX_DIM)
    height: int = Field(DEFAULT_DIM, ge=MIN_DIM, le=MAX_DIM)
    steps: int = Field(DEFAULT_STEPS, ge=1, le=20)
    guidance_scale: float = Field(DEFAULT_GUIDANCE, ge=0.0, le=10.0)
    seed: Optional[int] = None


@app.get("/healthz")
async def healthz():
    used = (torch.cuda.memory_allocated() / 1024**2) if torch.cuda.is_available() else 0
    return {
        "status": "ok" if pipe is not None else "loading",
        "model": MODEL_ID,
        "model_loaded": pipe is not None,
        "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
        "vram_mb_used": round(used, 1),
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
    try:
        gen = None
        if req.seed is not None:
            gen = torch.Generator(device="cuda").manual_seed(int(req.seed))
        async with gen_lock:
            # Run on a worker thread so the event loop stays responsive.
            def _gen():
                with torch.inference_mode():
                    out = pipe(
                        req.prompt,
                        negative_prompt=req.negative_prompt,
                        num_inference_steps=req.steps,
                        guidance_scale=req.guidance_scale,
                        height=req.height,
                        width=req.width,
                        generator=gen,
                    )
                    img = out.images[0]
                    buf = io.BytesIO()
                    img.save(buf, format="PNG", optimize=True)
                    return buf.getvalue()
            png = await asyncio.to_thread(_gen)
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
                "X-Steps": str(req.steps),
            },
        )
    except Exception as e:
        metrics["errors_total"] += 1
        logger.exception("generate failed")
        # Self-heal the one fault that does not recover on its own.
        #
        # Under GPU pressure the diffusers pipeline can end up with fp16
        # activations meeting an fp32 bias and every subsequent request dies
        # with "Input type (c10::Half) and bias type (float) should be the
        # same" -- permanently. /healthz keeps reporting ok because the model
        # object is still loaded, so nothing notices; on 2026-08-27 this
        # silently stopped an 8,000-image backfill twice, and only a manual
        # `systemctl restart` cleared it. Rebuild the pipeline in place
        # instead of waiting for a human.
        if "bias type" in str(e) or "c10::Half" in str(e):
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
