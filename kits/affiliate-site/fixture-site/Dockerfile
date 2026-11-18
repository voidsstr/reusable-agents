# The gate fixture's production image, built the way a site's Dockerfile.azure
# builds (deploy/Dockerfile.azure.tmpl): runtime dependencies only, the server
# compiled with tsc, the kit's browser bundle built in the image, the kit's
# migrations beside it. Build context: the kit root (kits/affiliate-site), so
# the fixture imports the kit's modules exactly as they are in the tree
# (fixture-site/Dockerfile.dockerignore keeps node_modules and test output out).
#
#   docker compose -f fixture-site/docker-compose.yml -f fixture-site/docker-compose.test.yml build backend-test
ARG NODE_IMAGE=node:22-alpine

# ---- runtime dependencies only (no devDependencies) ----
FROM ${NODE_IMAGE} AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund && npm cache clean --force

# ---- build: the fixture server, the kit modules it imports, the client bundle ----
FROM ${NODE_IMAGE} AS build
WORKDIR /kit
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY tsconfig.json ./
COPY server ./server
COPY client ./client
COPY scripts ./scripts
COPY fixture-site/tsconfig.json fixture-site/tsconfig.build.json ./fixture-site/
COPY fixture-site/src ./fixture-site/src
RUN node scripts/build-client.mjs --outdir /out/client \
 && npx tsc -p fixture-site/tsconfig.build.json --outDir /out/js \
 && test -f /out/js/fixture-site/src/server.js

# ---- runtime ----
FROM ${NODE_IMAGE} AS production
WORKDIR /app
ENV NODE_ENV=production \
    PORT=8080 \
    KIT_CLIENT_FILE=/app/client/dist/kit-client.js
COPY --from=deps /app/node_modules ./node_modules
COPY --from=build /out/js ./
COPY --from=build /out/client ./client/dist
COPY migrations ./migrations
COPY fixture-site/site.json fixture-site/site.violations.json fixture-site/llms.json fixture-site/llms.violations.json ./fixture-site/
COPY fixture-site/db ./fixture-site/db
COPY fixture-site/public ./fixture-site/public
# Files keep their build-context modes; a checkout made under umask 007 (0660
# files) would leave them unreadable to the node user.
RUN chmod -R a+rX /app/migrations /app/fixture-site
USER node
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=6 \
  CMD node -e "fetch('http://127.0.0.1:8080/health').then((r)=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
CMD ["node", "fixture-site/src/server.js"]
