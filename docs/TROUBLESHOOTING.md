# Troubleshooting

## Bot is silent / only replies to `/start`

### 1. Check the whitelist
```bash
sudo docker logs telegram-bot --tail 200 | grep "whitelist\|LOCKDOWN\|rejected"
```

If `LOCKDOWN` — your `ALLOWED_USER_IDS` and `ALLOWED_USERNAMES` are empty.
Open `/opt/telegram-bot/.env` and add your user_id.

### 2. Check llama-server
```bash
curl -s -m 5 http://localhost:8080/health
# → {"status":"ok"}
```

If it does not reply, the bot is **not at fault**. Start llama-server (see
[vaskes/llama.cpp-rocm-780m](https://github.com/vaskes/llama.cpp-rocm-780m)).

## `httpx.ConnectError` in the logs

Typical causes:

### a) llama-server is not running
See above.

### b) IPv6 vs IPv4 (rare, but happens in docker)
There is a monkey-patch in `bot.py` that filters IPv6. If you see this
error after editing, verify the monkey-patch is **not** removed:

```python
import socket
_orig_getaddrinfo = socket.getaddrinfo
def _ipv4_only_getaddrinfo(host, *args, **kwargs):
    results = _orig_getaddrinfo(host, *args, **kwargs)
    family = kwargs.get('family', socket.AF_UNSPEC)
    if family == socket.AF_UNSPEC:
        return [r for r in results if r[0] == socket.AF_INET] or results
    return results
socket.getaddrinfo = _ipv4_only_getaddrinfo
```

### c) Bot token revoked / invalid
`@BotFather` → verify the token.

## Bot writes `❌ Error: ...`

```bash
sudo docker logs telegram-bot --tail 50
```

Common errors:

- `Connection refused` to `localhost:8080` — llama-server is on a different port or not running
- `name resolution failed` — DNS inside the container, set `network_mode: host`
  (default), or add `dns: [8.8.8.8]` in compose
- `Model not found` — the model in `MODEL` env does not match the `--alias` on the server

## SearXNG returns 0 results

This is a **known issue** on cloud IPs — `DuckDuckGo`, `Brave`, `Startpage`
return CAPTCHA. On a localhost (home IP) it works.

Workarounds:
- Use only `get_weather` for weather questions
- Use `searxng_engines` to see the available list
- For Russian search, try `searxng_fetch_url` to a specific site
  (e.g. `https://www.google.com/search?q=...`)

## Whisper does not recognize voice

```bash
sudo docker logs whisper-api --tail 20
```

- Model is still downloading — first start pulls 1.5 GB, ~5-10 min
- Voice is too quiet / noisy — faster-whisper handles it poorly
- Language — if `language: 'ru'` is hardcoded in `bot.py` and the voice
  is in English, recognition will be off. Change to `language: 'auto'`
  (if upstream supports) or remove `language` entirely

## `docker compose build` fails with a pip error

```bash
# clear cache
sudo docker builder prune

# or explicitly pull a fresh python:3.11-slim
sudo docker pull python:3.11-slim
```

## Container does not start / goes into a restart loop

```bash
sudo docker logs telegram-bot --tail 100
```

If you see `Restarting` — likely an invalid `BOT_TOKEN` or llama-server
unreachable **at init time** (the bot does `get_me()` at startup).

## Networking between containers does not work

Make sure `network_mode: "host"` is in `docker-compose.yml`. Without it,
the container is isolated, and `localhost:8080` ≠ host `localhost:8080`.

## System limits

If the bot stops replying after N messages:

```bash
ulimit -n          # fd limit
df -h /opt         # disk
free -h            # memory
```

A llama-server with a large model and 8K context can eat all RAM. Watch
`MEM%` in `docker stats`.
