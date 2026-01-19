# Twitch EventSub Discord Announcer

Small always-on Python 3.11 service that monitors Twitch stream online/offline events for a list of channels and posts announcements to Discord via webhooks. It uses Twitch EventSub WebSocket transport (no inbound HTTP server) and is meant to run on a home NUC with Docker.

## Features

- Twitch EventSub WebSocket transport with auto-reconnect and resubscribe.
- OAuth client credentials flow for app access token.
- WebSocket EventSub subscriptions require a **user access token** (`TWITCH_USER_ACCESS_TOKEN`).
- Resolve Twitch login names to user IDs at startup (cached in memory).
- Announce `stream.online` events to Discord via webhook.
- Optional `stream.offline` announcements.
- Multiple “characters” routing to different Discord webhook URLs.
- Message templates defined in `config.yaml` (no code edits needed).
- Optional `state.json` persistence to avoid duplicate notifications on reconnect.

## Setup

### 1) Create a Twitch developer app

1. Go to the [Twitch Developer Console](https://dev.twitch.tv/console/apps).
2. Create an application (any name, set a dummy OAuth redirect URL).
3. Copy the **Client ID** and **Client Secret**.

You still need a **user access token** for EventSub WebSocket subscriptions. The Helix app token is only used for user/stream lookups.

### 2) Create Discord webhooks

1. In your Discord server, create webhooks for your desired channels/characters.
2. Copy the webhook URLs.

### 3) Configure the service

Edit `config.yaml` to list channels and template messages.

Example template fields:
- `{login}`: Twitch login name
- `{display_name}`: Twitch display name
- `{url}`: `https://twitch.tv/{login}`
- `{title}`: Stream title (only on online event)
- `{game}`: Stream game name (only on online event)

Environment variables referenced in `config.yaml` are expanded at runtime (e.g. `${TWITCH_CLIENT_ID}`).

> **Tip:** Use a local `.env` file for secrets and add it to `.gitignore` so you never commit tokens.

## How to run

### Local venv

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export TWITCH_CLIENT_ID=your_client_id
export TWITCH_CLIENT_SECRET=your_client_secret
export TWITCH_USER_ACCESS_TOKEN=your_user_access_token
export DISCORD_SYSTEM_WEBHOOK=https://discord.com/api/webhooks/...
export DISCORD_WEBHOOK_LOOP_TRACE=https://discord.com/api/webhooks/...
export DISCORD_WEBHOOK_PACKET_GHOST=https://discord.com/api/webhooks/...
export DISCORD_WEBHOOK_REDACTED=https://discord.com/api/webhooks/...
export DISCORD_WEBHOOK_AUX_PROC=https://discord.com/api/webhooks/...

python main.py
```

### Docker Compose

```bash
export TWITCH_CLIENT_ID=your_client_id
export TWITCH_CLIENT_SECRET=your_client_secret
export TWITCH_USER_ACCESS_TOKEN=your_user_access_token
export DISCORD_SYSTEM_WEBHOOK=https://discord.com/api/webhooks/...
export DISCORD_WEBHOOK_LOOP_TRACE=https://discord.com/api/webhooks/...
export DISCORD_WEBHOOK_PACKET_GHOST=https://discord.com/api/webhooks/...
export DISCORD_WEBHOOK_REDACTED=https://discord.com/api/webhooks/...
export DISCORD_WEBHOOK_AUX_PROC=https://discord.com/api/webhooks/...

docker compose up -d --build
```

The service will connect to EventSub, subscribe to your channels, and post go-live announcements.

## Behavior Notes

- `stream.online` events fetch stream title/game data from Helix for message formatting.
- The service tracks last-known live status in `state.json` to avoid duplicate posts on reconnect.
- If a channel does not specify a `character`, it uses `discord.system_webhook`.

## File Overview

- `main.py`: orchestrator and event handling.
- `twitch_eventsub.py`: WebSocket connection, subscriptions, event parsing.
- `twitch_helix.py`: OAuth token, user lookups, stream info.
- `discord_webhook.py`: webhook send with rate-limit handling.
- `config.py`: load `config.yaml`, expand `${ENV_VAR}` values.
- `state.json`: optional persistence of last-known live status.
