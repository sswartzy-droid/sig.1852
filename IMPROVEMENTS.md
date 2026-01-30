# Potential Improvements

Tracked list of future enhancements and ideas for sig.1852.

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
