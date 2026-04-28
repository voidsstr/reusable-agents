# Multi-stage build: node compiles + bundles the React app, then nginx
# serves the static dist + reverse-proxies /api and /ws to the API service.

FROM node:20-slim AS build
WORKDIR /app

# Allow VITE_API_BASE_URL to be set at build time so the bundle knows where
# to find the API. If unset, the UI hits its own origin and relies on nginx
# to reverse-proxy (see nginx.conf below).
ARG VITE_API_BASE_URL=""
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL

COPY framework/ui/package.json framework/ui/package-lock.json* ./
RUN npm install --no-audit --no-fund

COPY framework/ui/. ./
RUN npm run build


FROM nginx:1.27-alpine AS runtime

# Healthcheck deps + envsubst (gettext)
RUN apk add --no-cache curl gettext

# Static bundle
COPY --from=build /app/dist /usr/share/nginx/html

# Templated reverse-proxy config (rendered at startup with envsubst)
COPY framework/ui/nginx.conf.template /etc/nginx/templates/default.conf.template

# Default upstream — Docker compose service name. Override with
# AGENT_API_UPSTREAM=localhost:8090 for Azure Container Apps
# multi-container deployments.
ENV AGENT_API_UPSTREAM=agent-api:8090

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -fsS http://localhost/ > /dev/null || exit 1
