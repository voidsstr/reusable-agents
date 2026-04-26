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

# Healthcheck deps
RUN apk add --no-cache curl

# Static bundle
COPY --from=build /app/dist /usr/share/nginx/html

# Reverse-proxy config
COPY framework/ui/nginx.conf /etc/nginx/conf.d/default.conf

# Run as non-root (nginx-alpine ships an `nginx` user by default)
EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -fsS http://localhost/ > /dev/null || exit 1
