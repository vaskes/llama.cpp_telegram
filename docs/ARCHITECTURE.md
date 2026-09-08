# Architecture

## bot.py — what's inside

### Tool-calling loop

The bot **runs its own** tool-calling loop (up to 5 iterations). This is
because llama-server with `--mcp-servers-config` only loads the **list
of tools**, but does **not** execute them — that is the client's job
(us).

The cycle:

```
1. Fetch tools with GET /tools  (once, cached)
2. POST /v1/chat/completions with messages + tools
3. If finish_reason == "tool_calls":
     a. For each tool_call:
        - parse name + arguments
        - execute (HTTP to SearXNG, wttr.in, etc.)
        - append to messages: {role: "tool", tool_call_id, content}
     b. goto 2
4. Return content as the reply
```

### Which tools the bot can execute

| Tool | Implementation | Available when |
|---|---|---|
| `get_weather` | HTTP GET `https://wttr.in/{location}?format=j1&lang=ru` | Always (custom, added in code) |
| `searxng_search` | HTTP GET `http://SEARXNG_URL/search?q=...&format=json` | SearXNG is up |
| `searxng_fetch_url` | HTTP GET to URL + HTML strip | Always (if the internet is up) |
| `searxng_engines` | HTTP GET `http://SEARXNG_URL/engines` | SearXNG is up |
| `read_file`, `write_file`, `edit_file`, `exec_shell_command` | — | **DISABLED** in `DISABLED_TOOLS` |
| `playwright_browser_*` | — | **DISABLED** in `DISABLED_TOOLS` (needs Playwright MCP) |
| `file_glob_search`, `grep_search`, `get_info` | — | **DISABLED** (security + not implemented) |

### IPv4 monkey-patch

```python
socket.getaddrinfo = _ipv4_only_getaddrinfo
```

**Why:** the docker container has no IPv6 routing, but `api.telegram.org`
resolves to IPv6 (AAAA) first. `httpx`-based clients (Telegram Bot API,
SearXNG) fail with `Network is unreachable` on outbound. The monkey-patch
filters IPv6 out of `getaddrinfo` results, leaving only IPv4.

**When to remove:** once the host has IPv6 routing, or once upstream
fixes the A-record for `api.telegram.org`.

### Whitelist

```python
ALLOWED_USER_IDS = {int(x) for x in ENV.split(',') if x.strip().isdigit()}
ALLOWED_USERNAMES = {x.lstrip('@').lower() for x in ENV.split(',') if x.strip()}

if not ALLOWED_USER_IDS and not ALLOWED_USERNAMES:
    LOCKDOWN = True
```

Each handler starts with:
```python
if await reject_if_unauthorized(update, context):
    return
```

`reject_if_unauthorized`:
- If `LOCKDOWN` → reject
- If `user_id` in `ALLOWED_USER_IDS` → allow
- If `username.lower()` in `ALLOWED_USERNAMES` → allow
- Otherwise → log `[SECURITY] rejected id=...` + `return True` (handler exits silently)

**Silent rejection** (no `reply_text`) — so a stranger does not learn
the bot exists. If the bot replied "access denied", that would confirm
the bot is alive.

## whisper-api

Uses image `fedirz/faster-whisper-server:latest-cpu` — a wrapper around
`faster-whisper` by SYSTRAN, OpenAI-compatible API.

Default model `Systran/faster-distil-whisper-large-v3` (1.5 GB) — a good
balance of speed/quality for Russian. The model is downloaded on first
start to `/opt/whisper-api/cache/`.

If you need better accuracy, set `WHISPER_MODEL` in `.env` to
`Systran/faster-whisper-large-v3` (~3 GB, slower).

## Network model

`network_mode: "host"` in `telegram-bot/docker-compose.yml`.

**Pros:**
- Bot sees `localhost:8080` (llama), `localhost:8000` (whisper), `localhost:8888` (searxng)
  without any extra DNS or links
- Easier to debug (`netstat`, `ss` on the host = what the bot sees)

**Cons:**
- Container is not isolated from the host network
- A port the bot would listen on (if it did) would take a host port
- You cannot run two bot instances with the same `BOT_TOKEN`

For a production-grade setup, switch to a bridge network + explicit
internal DNS names (`http://llama:8080/v1`). For a single standalone
machine, this is overkill.
