# Shoutouts

One file per streamer, named `{login}.txt` (lowercase Twitch username).

Each file can contain one or more template blocks separated by a blank line. A random block is chosen each time a shoutout fires — manual `!so` or auto-shout on first chat message.

## Available variables

| Variable | Value |
|---|---|
| `{login}` | Twitch username (lowercase) |
| `{display_name}` | Display name from Twitch API |
| `{url}` | `twitch.tv/{login}` |
| `{title}` | Current stream title (triggers Helix API call) |
| `{game}` | Current game being played (triggers Helix API call) |

## Example — rubyhaven.txt

```
>> signal detected: {display_name} — {game}
twitch.tv/{login}

loop.trace: rubyhaven frequency located. signal strong.
twitch.tv/{login}
```

## Fallback

If no file exists for a username, the `shoutout_fallback` template in `config.yaml` is used.
To add someone to the auto-shout list without a custom file, add their login to `auto_shout.channels` in `config.yaml`.
