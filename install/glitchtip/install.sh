#!/usr/bin/env bash
# install.sh — bring up GlitchTip locally as the framework's optional
# error-tracking sidecar. Idempotent: re-running just refreshes images.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

if [ ! -f .env ]; then
  cp .env.example .env
  SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(50))')"
  # macOS sed needs -i '' but this repo is linux-only per CLAUDE.md
  sed -i "s|^GLITCHTIP_SECRET_KEY=.*|GLITCHTIP_SECRET_KEY=${SECRET}|" .env
  echo "Generated .env with a fresh GLITCHTIP_SECRET_KEY."
fi

docker compose -f docker-compose.glitchtip.yml pull
docker compose -f docker-compose.glitchtip.yml up -d --remove-orphans

echo
echo "GlitchTip starting at http://localhost:8095"
echo "First load takes ~30s while migrations run; re-check if it 502s."
echo
echo "Next steps:"
echo "  1. Open http://localhost:8095 and register the operator account"
echo "  2. Create org + project (React Native)"
echo "  3. Copy the DSN — set as EAS secret SENTRY_DSN in mobile/"
echo "  4. User → Profile → Auth Tokens → create with"
echo "     org:read project:read event:read member:read"
echo "  5. Export in framework .env:"
echo "       SENTRY_API_TOKEN=<token>"
echo "       SENTRY_ORG_SLUG=<org>"
echo "       SENTRY_PROJECT_SLUG=<proj>"
echo "       SENTRY_API_BASE=http://localhost:8095/api/0"
echo "  6. systemctl --user enable --now agent-crash-watcher-agent.timer"
