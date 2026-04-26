# Multi-stage build for the framework API.
# Stage 1 installs Python deps; Stage 2 is the runtime image (slim).

FROM python:3.12-slim AS deps
WORKDIR /build
COPY framework/api/requirements.txt /build/requirements.txt
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.12-slim AS runtime

# Non-root user (no specific privilege requirements — runs API only)
RUN useradd -u 1000 -m -s /bin/bash agent

# OS deps: curl for healthchecks, git for release_tagger calls (only useful
# if the container has access to the repo dir via bind mount; harmless otherwise)
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy installed site-packages from deps stage
COPY --from=deps /install /usr/local

# Copy framework source + shared/ (schemas + helpers used by framework.core)
WORKDIR /app
COPY framework /app/framework
COPY shared /app/shared
RUN chown -R agent:agent /app

USER agent

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    LOG_LEVEL=INFO

EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -fsS http://localhost:8090/api/health || exit 1

CMD ["uvicorn", "framework.api.app.main:app", "--host", "0.0.0.0", "--port", "8090"]
