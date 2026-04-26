# TikTok Server

VPS-deployed FastAPI service for the anime-tiktok-reproducer mobile flow.

## Quickstart (local dev)

```bash
cd server
uv venv
source .venv/bin/activate
uv pip install -e ".[dev]"
cp .env.example .env                    # fill in real values
cp config/config.example.yaml config/config.yaml   # fill in real values
uv run uvicorn app.main:app --reload
```

Tests:
```bash
uv run pytest
```

## Deployment

See `Dockerfile`, `docker-compose.yml`, and `Caddyfile`. Deployed at `tiktok.sididi.tv`.

## Deployment (VPS)

1. SSH into the VPS, install Docker + docker-compose-plugin and Caddy.
2. Clone or sparse-checkout the `server/` subtree.
3. Copy `.env.example` → `.env` and `config/config.example.yaml` → `config/config.yaml`; fill in real values.
4. Place real avatar files in `avatars/`.
5. Add the `Caddyfile` snippet to `/etc/caddy/Caddyfile`; reload Caddy: `systemctl reload caddy`.
6. Bring the service up: `docker compose up -d --build`.
7. Verify: `curl https://tiktok.sididi.tv/healthz`.

## Update flow

```bash
git pull
docker compose up -d --build
```
