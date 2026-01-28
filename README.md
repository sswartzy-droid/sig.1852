# Twitch Stream Discord Announcer

Small always-on Python 3.11 service that monitors Twitch streams for a list of channels and posts announcements to Discord via webhooks. It polls the Twitch Helix API on a configurable interval and is meant to run on a home NUC with Docker.

## Features

- Polls Twitch Helix API for live stream status with configurable interval.
- OAuth client credentials flow for app access token with proactive refresh before expiry.
- Resolve Twitch login names to user IDs at startup (batched, cached in memory).
- Announce stream online events to Discord via webhook.
- Multiple "characters" routing to different Discord webhook URLs.
- Message templates defined in `config.yaml` (no code edits needed).
- `state.json` persistence to avoid duplicate notifications on restart.
- Graceful shutdown on SIGTERM/SIGINT with state flush.
- Exponential backoff on consecutive polling failures.
- Rate-limit-aware Discord webhook sender with capped retries.
- Random quote drip feature with daily quotas and configurable posting windows.
- Docker health check based on last successful poll timestamp.
- Configurable log level via `LOG_LEVEL` environment variable.

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
# ... other character webhooks

docker compose up -d --build
```

The service will start polling Twitch and post go-live announcements to Discord.

## Behavior Notes

- The poller fetches live streams every 90 seconds (configurable via `polling.interval_seconds`).
- Stream online detection uses the `started_at` timestamp to avoid duplicate posts.
- If a channel does not specify a `character`, it uses `discord.system_webhook`.
- Quote posting respects `window_start`/`window_end` hours from config.
- State is persisted atomically to `data/state.json` to prevent corruption.
- On consecutive polling failures, the interval increases exponentially (up to 8x).
- The Docker health check verifies the last successful poll was within the last 5 minutes.
- Set `LOG_LEVEL` environment variable to `DEBUG`, `INFO`, `WARNING`, or `ERROR`.

## File Overview

- `main.py`: orchestrator, state management, graceful shutdown.
- `twitch_polling.py`: poll loop, stream change detection, announcements.
- `twitch_helix.py`: OAuth token (with expiry tracking), user lookups, stream info.
- `discord_webhook.py`: webhook send with rate-limit and error retry caps.
- `quote_drip.py`: random quote scheduler with posting windows and content filtering.
- `config.py`: load `config.yaml`, expand `${ENV_VAR}` values.
- `data/state.json`: runtime persistence of last-known live status and quote progress.
