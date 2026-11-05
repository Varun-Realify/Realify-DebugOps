# Installation

DebugOps runs as a Docker Compose stack on one Linux host. It needs no developer laptop and no personal credentials: GitHub access goes through Realify's GitHub App, and AI access through a dedicated Anthropic key.

## 1. Host

| | Minimum | Recommended |
|---|---|---|
| OS | Linux with Docker Engine 24+ and Compose v2 | Ubuntu 24.04 LTS |
| CPU / RAM | 4 vCPU / 8 GB | 8 vCPU / 16 GB (more for concurrent runs) |
| Disk | 40 GB | 100 GB SSD |
| Network | Outbound HTTPS to GitHub, api.anthropic.com, PyPI/npm, api.osv.dev; inbound HTTPS for webhooks | |

**Use a dedicated host or VM.** The worker starts sandbox containers through a Docker API proxy. Access to Docker is root-equivalent on that host (see [SECURITY.md](SECURITY.md)), so do not co-locate unrelated workloads.

Each concurrent scan or debugging run uses one sandbox (default limits: 2 CPU, 2 GB RAM). One worker processes one job at a time. Scale out with `docker compose up -d --scale worker=N`.

## 2. Get the code and configure

```bash
git clone <your internal repo url> debugops && cd debugops
cp .env.example .env && chmod 600 .env
```

Edit `.env`:

| Variable | Value |
|---|---|
| `POSTGRES_PASSWORD` | `openssl rand -hex 24` |
| `SECRET_KEY` | `openssl rand -hex 32` |
| `COOKIE_SECURE` | `true` (requires HTTPS, see step 4) |
| `GITHUB_*` | from the GitHub App ([GITHUB_APP.md](GITHUB_APP.md)) |
| `ANTHROPIC_API_KEY` | a dedicated, spend-limited key from an Anthropic workspace created for DebugOps |
| `WEB_BIND` / `WEB_PORT` | keep `127.0.0.1:8088` behind the reverse proxy |

Put the GitHub App private key at `secrets/github-app.pem` (`chmod 600`).

## 3. Start

```bash
docker compose up -d --build          # first build takes several minutes (sandbox image)
docker compose ps                      # api healthy; sandbox-image "Exited (0)" is expected
docker compose exec api python -m app.cli create-admin --email you@realify.ai --name "Your Name"
docker compose exec api python -m app.cli github-check
```

## 4. HTTPS (required for production)

Terminate TLS in front of `web`. A minimal Caddy example (it obtains certificates automatically):

```
debugops.realify.ai {
    reverse_proxy 127.0.0.1:8088
}
```

With nginx or a cloud load balancer, forward `Host`, allow WebSocket upgrades on `/api/v1/ws`, and allow request bodies up to 25 MB on `/api/v1/github/webhook`. Behind a proxy that adds `X-Forwarded-For`, configure `set_real_ip_from` in `frontend/nginx.conf` so audit logs record the real client IP.

## 5. Connect GitHub and verify

1. Follow [GITHUB_APP.md](GITHUB_APP.md): create the App, install it on selected repositories, Sync, **Test access**, then **Enable**.
2. Run a scan: Repositories, then **Scan now**.
3. Run the smoke test from any machine with the repository checked out:

   ```bash
   cd backend && uv sync && DEBUGOPS_SMOKE_PASSWORD=... uv run python ../scripts/e2e_smoke.py \
     --url https://debugops.realify.ai --email you@realify.ai
   ```

## 6. First debugging run

Open a bug and click **Investigate with AI**. Watch the run under **Runs**. When validation passes, a Reviewer approves PR creation. DebugOps opens the PR, and a human merges it in GitHub.

## Upgrading

```bash
scripts/backup.sh
git pull
docker compose up -d --build     # the api applies database migrations on start
docker compose exec api python -m app.cli github-check
```

Roll back by checking out the previous version and restoring the backup taken before the upgrade (see [OPERATIONS.md](OPERATIONS.md)).
