# sig.1852 / CODA

## What this is
Cosmic horror Twitch/Discord bot. Lives on the NUC (CT100 LXC, Docker).
Persona: CODA — an eldritch entity that observes and occasionally speaks.
Current home: twitch.tv/reburve

## Codebase
- Repo: https://github.com/sswartzy-droid/sig.1852
- Local copy: `~/Documents/Claude-Workspace/Claude/Projects/sig.1852/` (on `claude/brb-back-commands` branch)
- Production branch: `prod` (running on NUC via Docker Compose)
- Language: Python 3.11

## Deployment
Running on NUC CT100 LXC at 192.168.0.11.
Start/restart: `docker compose up -d` from repo directory on NUC.
Pull latest: `git pull` on NUC, then rebuild.

## Current state
Bot active and running in production. Updated 2026-04-11.
Multiple claude/ branches (brb-back-commands, webhook-routing-update) contain work not yet
consolidated into prod — but fixes are deployed. Bot is healthy.

## What's working
- [x] Stream go-live detection and Discord announcements
- [x] Multi-character quote drip (5 CODA characters)
- [x] !brb / !back commands wired to BrbFeed (2026-04-10)
- [x] Webhook routing fix — go-live events post to correct Discord channel (2026-04-11)
- [x] Graceful shutdown, health endpoint, config hot-reload, OAuth token refresh

## Outstanding items
- [ ] OBS text source reading intermission.txt + live test
- [ ] MixItUp → direct API migration (longer term)

## The pipeline (designed, not built)
Architecture designed April 2026. Key components:
- Context aggregator with rolling window (~60-90 sec)
- Two Whisper STT instances: `[HOST]` (mic) and `[GAME]` (game audio)
- OBS websocket listener (port 4455) tagged as `[SCENE]`
- Twitch chat monitor tagged as `[CHAT]`
- GPU headroom check via `rocm-smi` before firing Ollama
- Ollama local LLM with CODA persona + lore in system prompt
- Rate limiter: min 45 sec between unprompted responses, max 2 per 5 min
- Outputs: Twitch chat, OBS scene triggers, Discord lore log

## Rules for this project
- Never touch the live running bot process without explicit permission
- Rate limiter is non-negotiable — do not remove or bypass it
- Test locally before any push
- Work in claude/ branch, never push directly to prod
