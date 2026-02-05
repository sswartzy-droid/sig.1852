# Twitch Stream Discord Announcer

Small always-on Python 3.11 service that monitors Twitch streams for a list of channels and posts announcements to Discord via webhooks. It polls the Twitch Helix API on a configurable interval and is meant to run on a home NUC with Docker or as a standalone process.

## Features

- Polls Twitch Helix API for live stream status with configurable interval.
- OAuth client credentials flow for app access token with proactive refresh before expiry.
- Resolve Twitch login names to user IDs at startup (batched in groups of 100).
- Announce stream online events to Discord via webhook.
- Multiple "characters" routing to different Discord webhook URLs.
- Weighted character selection for quote posting (configurable per-character weights).
- Message templates defined in `config.yaml` with safe variable substitution.
- `state.json` atomic persistence to avoid duplicate notifications on restart.
- Graceful shutdown on SIGTERM/SIGINT with state flush.
- Exponential backoff on consecutive polling failures (up to 8x multiplier).
- Rate-limit-aware Discord webhook sender with capped retries; raises `WebhookSendError` on final failure.
- Random quote drip feature with daily quotas (hard cap of 3/day), random scheduling across the full 24-hour local day.
- Quote content filtering: max length, max sentences, no Discord mentions, no links.
- Config hot-reload on file change (validated before applying).
- Structured JSON logging option (`LOG_FORMAT=json`) with rotating file log support.
- HTTP health endpoint for monitoring (works with and without Docker).
- Docker container runs as non-root user.

## Health Endpoint

The service exposes an HTTP health endpoint at `GET /health` for monitoring.

**Configuration via environment variables:**

| Variable | Default | Description |
|---|---|---|
| `HEALTH_HOST` | `127.0.0.1` | Bind address for the health server |
| `HEALTH_PORT` | `8080` | Port for the health server |
| `HEALTH_STALE_SECONDS` | `300` | Seconds before a missed poll is considered stale |

**For non-Docker deployments**, set `HEALTH_HOST=0.0.0.0` to allow external access, then poll with curl or a cron job:

```bash
curl -sf http://localhost:8080/health
```

The endpoint returns JSON with status `"ok"` (HTTP 200) or `"stale"` (HTTP 503):

```json
{
  "status": "ok",
  "uptime_seconds": 3600.1,
  "last_poll_at": 1706745600.0,
  "poll_age_seconds": 45.2,
  "channels_live": ["rubyhaven"],
  "channels_live_count": 1,
  "quotes_today": 1,
  "quotes_quota": 2,
  "quotes_next_at": 1706780000.0
}
```

A startup grace period prevents false alarms before the first poll completes.

**Inside Docker**, the `docker-compose.yml` healthcheck uses this same endpoint automatically.

## Setup

### 1) Create a Twitch developer app

1. Go to the [Twitch Developer Console](https://dev.twitch.tv/console/apps).
2. Create an application (any name, set a dummy OAuth redirect URL).
3. Copy the **Client ID** and **Client Secret**.

### 2) Create Discord webhooks

1. In your Discord server, create webhooks for your desired channels/characters.
2. Copy the webhook URLs.

### 3) Configure the service

Edit `config.yaml` to list channels and template messages.

Template fields:
- `{login}`: Twitch login name
- `{display_name}`: Twitch display name
- `{url}`: `https://twitch.tv/{login}`
- `{title}`: Stream title (only on online event)
- `{game}`: Stream game name (only on online event)

Environment variables referenced in `config.yaml` are expanded at runtime (e.g. `${TWITCH_CLIENT_ID}`).

### 4) Run with Docker Compose

```bash
export TWITCH_CLIENT_ID=your_client_id
export TWITCH_CLIENT_SECRET=your_client_secret
export DISCORD_WEBHOOK_SYSTEM=https://discord.com/api/webhooks/...
export DISCORD_WEBHOOK_LOOP_TRACE=https://discord.com/api/webhooks/...
export DISCORD_WEBHOOK_PACKET_GHOST=https://discord.com/api/webhooks/...
export DISCORD_WEBHOOK_REDACTED=https://discord.com/api/webhooks/...
export DISCORD_WEBHOOK_AUX_PROC=https://discord.com/api/webhooks/...
export DISCORD_WEBHOOK_CORE_AUDIT=https://discord.com/api/webhooks/...

docker compose up -d --build
```

### 5) Run without Docker

```bash
pip install -r requirements.txt
export TWITCH_CLIENT_ID=your_client_id
# ... set all env vars ...
python main.py
```

Set `HEALTH_HOST=0.0.0.0` if you need the health endpoint reachable from other machines.

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `CONFIG_PATH` | `config.yaml` | Path to configuration file |
| `STATE_DIR` | `data` | Directory for state persistence |
| `HEALTH_HOST` | `127.0.0.1` | Health endpoint bind address |
| `HEALTH_PORT` | `8080` | Health endpoint port |
| `HEALTH_STALE_SECONDS` | `300` | Poll staleness threshold |
| `LOG_LEVEL` | `INFO` | Logging level (DEBUG, INFO, WARNING, ERROR) |
| `LOG_FORMAT` | `text` | Log format (`text` or `json`) |
| `LOG_FILE` | _(empty)_ | Optional rotating log file path |

## Behavior Notes

- The poller fetches live streams every 90 seconds (configurable via `polling.interval_seconds`).
- Stream online detection uses the `started_at` timestamp to avoid duplicate posts.
- If a channel does not specify a `character`, it uses `discord.system_webhook`.
- Quotes are posted at random times throughout the day, with a hard cap of 3 per day.
- Quote files use blank-line-separated blocks (multi-line quotes supported).
- State is persisted atomically to `data/state.json` to prevent corruption.
- On consecutive polling failures, the interval increases exponentially (up to 8x).
- Config changes are detected via file mtime and hot-reloaded with validation.

## File Overview

- `main.py`: orchestrator, state management, health server, logging, graceful shutdown.
- `twitch_polling.py`: poll loop, stream change detection, announcements, config hot-reload.
- `twitch_helix.py`: OAuth token management, user lookups, stream info (batched).
- `discord_webhook.py`: webhook send with rate-limit and error retry caps.
- `quote_drip.py`: random quote scheduler with weighted characters, content filtering.
- `config.py`: load `config.yaml`, expand `${ENV_VAR}` values, validate config.
- `data/state.json`: runtime persistence of last-known live status and quote progress.
