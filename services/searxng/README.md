# Local SearXNG

Image-search backend for the hero-image curators and the recipe image refiller.

## Why this is here

The agents pointed at `https://searxng.aisleprompt.com`. Its Cloudflare tunnel
origin went dead on **2026-08-25** (HTTP 530 / error 1033) and is defined in no
repo, so it could not be restored. Four agents depend on this backend and the
two hero-image curators reported `success (100%)` with `no_candidate` on every
article for three days — a dead dependency that never raised an alarm.

## Run it

```bash
cp config/settings.yml.example config/settings.yml
python3 -c "import secrets;print(secrets.token_hex(32))"   # paste as secret_key
docker run -d --name searxng --restart unless-stopped \
  -p 127.0.0.1:8888:8080 \
  -v "$PWD/config:/etc/searxng:rw" \
  -e SEARXNG_BASE_URL=http://127.0.0.1:8888/ \
  searxng/searxng:latest
```

## Traps

- **`json` is not an enabled format by default.** The agents call
  `/search?format=json`; without `search.formats: [html, json]` in
  `settings.yml` every query 403s and you get silent `no_candidate`.
- **The installed systemd unit decides `SEARXNG_URL`, not the agent's
  `run.sh`.** A unit generated from a manifest with the URL baked in ignores
  every per-agent default. Fix the manifest *and* the installed unit, or the
  change looks applied and does nothing.
- `~/.reusable-agents/secrets.env` is loaded by every agent unit as an
  `EnvironmentFile`, so its `SEARXNG_URL` overrides `${SEARXNG_URL:-...}`
  defaults. It is the one place that actually decides the backend.

Regression coverage: `tests/test_hero_curator_gate.py`.
