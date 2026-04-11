# sig.1852 / CODA

## What this is
Cosmic horror Twitch/Discord bot. Lives on the Y510p (Xubuntu minimal).
Persona: CODA — an eldritch entity that observes and occasionally speaks.
Current home: twitch.tv/reburve

## Codebase
- Repo: hosted on Y510p
- Active branch: `claude/` (needs merge to main — partially completed)
- Language: Python

## Current state
Bot is functional and running on Y510p.
Claude Code did an audit and fixes session — changes sit in `claude/` branch.
Git merge workflow partially completed, not finished.
MixItUp currently handles some integrations — longer term goal is direct Claude API.

## The pipeline (designed, not built)
Architecture designed in April 2026. Key components:
- Context aggregator with rolling window (~60-90 sec)
- Two Whisper STT instances: `[HOST]` (mic) and `[GAME]` (game audio)
- OBS websocket listener (port 4455) tagged as `[SCENE]`
- Twitch chat monitor tagged as `[CHAT]`
- GPU headroom check via `rocm-smi` before firing Ollama
- Ollama local LLM with CODA persona + lore in system prompt
- Rate limiter: min 45 sec between unprompted responses, max 2 per 5 min
- Outputs: Twitch chat, OBS scene triggers, Discord lore log

## Outstanding items
- [ ] Merge `claude/` branch to main
- [x] Fix chat module graceful handling (missing token no longer kills the service)
- [x] `!brb` / `!back` commands wired to BrbFeed (2026-04-10)
- [ ] OBS text source reading `intermission.txt` + live test
- [ ] Implement context aggregator module
- [ ] Whisper STT integration (two channels)
- [ ] OBS websocket integration
- [ ] Ollama integration with GPU headroom check
- [ ] Rate limiter
- [ ] MixItUp → direct API migration (longer term)

## Rules for this project
- Always work in `claude/` branch
- Never touch the live running bot process without explicit permission
- Rate limiter is non-negotiable — do not remove or bypass it
- Test locally before any push
