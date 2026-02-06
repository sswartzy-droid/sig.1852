# Potential Improvements

Tracked list of future enhancements and ideas for sig.1852.

---

## Recently Implemented

The following items have been addressed in the current codebase:

- **Safe template substitution** — `_SafeDict` prevents format string injection in go-live messages.
- **Webhook fallback** — Unknown characters fall back to `system_webhook` instead of raising `KeyError`.
- **Batched API calls** — `get_streams` batches internally (100 per request) matching `get_users`.
- **Config reload error messages** — `ValueError` from validation is logged separately from generic errors.
- **Health startup grace period** — Health endpoint reports healthy during startup before first poll completes.
- **Configurable config path** — `CONFIG_PATH` environment variable (defaults to `config.yaml`).
- **Single save_state on shutdown** — Removed double save from signal handler.
- **Cleaned up state migration** — Removed dead legacy migration code from `_ensure_state_shape`.
- **Quote exhaustion handling** — Quote loop sleeps until next day when all quotes are exhausted instead of spinning.
- **Health endpoint bind address** — Defaults to `127.0.0.1`; configurable via `HEALTH_HOST`.
- **Non-root Docker user** — Container runs as unprivileged `app` user.
- **Reduced save_state calls** — Single save via `finally` block in poll loop.
- **Weighted character selection** — Uses `random.choices` with configurable weights per character.
- **Safe env var parsing** — Module-level `int()` calls wrapped in try/except to avoid crashes before logging.
- **Typed Poller config** — `AppConfig` type instead of `Any`.
- **Removed deprecated docker-compose version key**.
- **WebhookSendError** — `DiscordWebhook.send` raises on final failure instead of silently returning.
- **Standalone `load_quotes`** — Extracted as a top-level function for independent testing.
- **Filter chain pattern** — Quote filtering uses composable `QuoteFilter` callables.
- **Quote index bounds check** — `_next_valid_quote` validates indices before access, preventing `IndexError` when quote files change.
- **Root health endpoint** — Added `/` route returning service info and available endpoints.

---

## Code Review Findings

The following items were identified during a thorough code review. Items marked ✅ have been fixed; others remain as future work.

### Bugs Fixed

| # | Issue | Status |
|---|-------|--------|
| 1 | Format string injection in `_format_message` via user-controlled template vars | ✅ Fixed — `_SafeDict` returns `{key}` for missing keys |
| 2 | `_resolve_webhook` raises `KeyError` on unknown character | ✅ Fixed — falls back to `system_webhook` with warning |
| 3 | `get_streams` doesn't batch internally (unlike `get_users`) | ✅ Fixed — batches 100 IDs per request |
| 4 | Config reload logs generic "Failed to reload" on validation error | ✅ Fixed — separate `ValueError` catch with specific message |
| 5 | Health endpoint returns stale before first poll completes | ✅ Fixed — grace period based on uptime |
| 6 | Hardcoded `config.yaml` path | ✅ Fixed — `CONFIG_PATH` env var |
| 7 | Double `save_state` on shutdown (signal handler + finally) | ✅ Fixed — removed from signal handler |
| 8 | Dead `_ensure_state_shape` migration code | ✅ Fixed — removed legacy migration |
| 9 | `DiscordWebhook.send` silently drops messages on error | Deferred — caller may want control |
| 10 | Quote loop spins when all quotes exhausted | ✅ Fixed — sets `_exhausted` flag, sleeps until next day |
| 11 | Health server binds to `0.0.0.0` by default | ✅ Fixed — defaults to `127.0.0.1`, `HEALTH_HOST` configurable |
| 12 | Missing `__all__` exports in modules | Deferred — low priority |
| 13 | Docker runs as root | ✅ Fixed — non-root `app` user |
| 14 | `save_state` called twice per poll cycle | ✅ Fixed — single save via `finally` block |
| 15 | `_weighted_character_order` is O(n²) | ✅ Fixed — `random.choices` with dedup |
| 16 | Module-level `int()` on env vars can crash before logging | ✅ Fixed — `_safe_int_env` helper |
| 17 | `Poller.config` typed as `Any` | ✅ Fixed — typed as `AppConfig` |
| 18 | No unit tests | Future work |
| 19 | Deprecated `version` key in docker-compose.yml | ✅ Fixed — removed |

### Opportunities Implemented

| # | Improvement | Status |
|---|-------------|--------|
| 13 | `WebhookSendError` exception on final failure | ✅ Implemented |
| 14 | Standalone `load_quotes()` function for testing | ✅ Implemented |
| 15 | Composable `QuoteFilter` chain pattern | ✅ Implemented |
| 16 | Multi-stage Docker build | Deferred — optional optimization |

### Strengths Noted

- Atomic `save_state` via `tempfile.mkstemp` + `os.replace`
- Config hot-reload with mtime checking
- Startup validation with aggregated error messages
- Exponential backoff with ceiling on poll failures
- Graceful shutdown via `asyncio.Event` and signal handlers
- Structured JSON logging toggle
- Rotating file log handler support
- Batched `get_users` (100 per request)
- Rate-limit retry cap in Discord webhook
- Proactive OAuth token refresh before expiry
- Health endpoint with comprehensive status JSON
- Weighted random character selection for quotes

---

## Go-Live Notifications

### Lore-Rich Shoutout Templates
Per-channel text files in a `shoutouts/` directory, each containing one or more announcement templates (separated by blank lines). On go-live, the poller picks a random template block from the file, formats it with `{login}`, `{display_name}`, `{url}`, `{title}`, `{game}`, and sends it via the channel's assigned character webhook.

- Missing file falls back to the default template
- Multiple blocks per file enable random variety per streamer
- Hot-reload picks up new/edited files automatically

### Multi-Message Announcements
Allow `template_online` to accept a list of strings instead of a single string. Each entry is sent as a separate Discord message in sequence. Use cases: lore shoutout followed by a practical link, or a role ping in a separate message.

```yaml
channels:
  - login: rubyhaven
    character: loop_trace
    template_online:
      - |
        SIGNAL INTERCEPT — {display_name} has breached containment.
        Stream origin locked: {game}
      - "Trace the signal: {url}"
```

### Per-Channel Character Routing
Already supported in code. Add `character` field to channel entries in `config.yaml` to route specific streamers' announcements to specific Discord webhooks/personas.

```yaml
channels:
  - login: rubyhaven
    character: loop_trace
  - login: arthice
    character: packet_ghost
  - login: mirasuriel
    # no character = uses system_webhook
```

---

## Discord Enhancements

### Rich Embeds
Replace plain text messages with Discord embed objects. Support color, thumbnail (Twitch stream preview), fields (game, title), and author info. The Helix stream response already includes `thumbnail_url` and `viewer_count`.

### Viewer Count Template Variable
Expose `{viewers}` from the Helix stream response as a template variable. Low priority unless there's a specific use case.

---

## Operational

### Multi-Stage Docker Build
Use a multi-stage Dockerfile to reduce final image size. First stage installs dependencies, second stage copies only the runtime artifacts. Saves disk on the NUC.

### Systemd Watchdog Integration
For non-Docker deployments, integrate with systemd's `WatchdogSec` by notifying systemd on each successful poll cycle. Provides automatic restart if the service hangs.

### Prometheus Metrics Endpoint
Expose `/metrics` in Prometheus format: poll count, announcement count, errors, quote posts, uptime. Enables dashboarding and alerting beyond the basic `/health` endpoint.

### State Backup Rotation
Keep the last N copies of `state.json` (timestamped) so a corrupted or bad state can be manually recovered. Currently only the latest is kept.

---

## Quote System

### Quote Cooldown per Character
Track recently posted quotes per character and enforce a minimum gap before the same quote can repeat, even after the index list cycles.

### Quote Preview/Test Command
A CLI flag (`python main.py --test-quote loop_trace`) that loads quotes, picks one, prints it, and exits. Useful for validating quote files without waiting for the scheduler.

---

## Configuration

### Channel Groups
Group channels by community or event, with shared templates and webhooks per group. Reduces config repetition for large channel lists.

### Config Schema Validation
Add a JSON Schema or pydantic model for `config.yaml` that catches typos and invalid structures at load time with precise error messages.
