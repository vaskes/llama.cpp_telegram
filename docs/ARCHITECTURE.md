# Architecture

## bot.py — what's inside

### Two code paths in `call_llama`

The function splits internally into a **streaming** path (text, voice,
document) and a **non-streaming** path (vision). They share only the
function signature and the return contract. See
[CALL_LLAMA.md](CALL_LLAMA.md) §1 for the full design.

### Tool-calling loop (streaming path only)

The bot **runs its own** tool-calling loop (up to 15 iterations, with
a wall-clock cap of 600 s, an identical-call detector, and a
hallucination abort). This is because llama-server with
`--mcp-servers-config` only loads the **list of tools**, but does
**not** execute them — that is the client's job (us).

The cycle:

```
1. Detect tool intent from user_text (see §2 in CALL_LLAMA.md)
2. If intent: req_tools = all_tools; else req_tools = None
3. POST /v1/chat/completions with messages + (optional) tools
4. If finish_reason == "tool_calls" AND req_tools:
     a. For each tool_call:
        - parse name + arguments
        - execute (HTTP to wttr.in / donsetch-http MCP)
        - append to messages: {role: "tool", tool_call_id, content}
     b. goto 3
5. If finish_reason == "tool_calls" AND NOT req_tools:
     → hallucination abort, return content or reasoning tail
6. Return content as the reply
```

### Which tools the bot can execute

| Tool | Implementation | Available when |
|---|---|---|
| `get_weather` | HTTP GET `https://wttr.in/{location}?format=j1` | Always (custom, added in code) |
| `web_search` | HTTP POST to donsetch-http MCP `/mcp` | donsetch-http is up |
| `web_fetch` | HTTP POST to donsetch-http MCP `/mcp` | donsetch-http is up |
| `web_crawl` | HTTP POST to donsetch-http MCP `/mcp` | donsetch-http is up |
| `web_screenshot` | HTTP POST to donsetch-http MCP `/mcp` | donsetch-http is up |
| `read_file`, `write_file`, `edit_file`, `exec_shell_command` | — | **DISABLED** in `DISABLED_TOOLS` |
| `file_glob_search`, `grep_search`, `get_info` | — | **DISABLED** (security + not implemented) |

### Polling loop

Bypasses `Application.run_polling()` from python-telegram-bot 21.0.
Hand-rolled `httpx` long-polling loop with `max_keepalive_connections=0`.
See [CALL_LLAMA.md](CALL_LLAMA.md) §4 for why.

### IPv4 monkey-patch

```python
socket.getaddrinfo = _ipv4_only_getaddrinfo
```

**Why:** the docker container has no IPv6 routing, but `api.telegram.org`
resolves to IPv6 (AAAA) first. `httpx`-based clients (Telegram Bot API,
donsetch MCP) fail with `Network is unreachable` on outbound. The
monkey-patch filters IPv6 out of `getaddrinfo` results, leaving only IPv4.

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
- Bot sees `localhost:8080` (llama), `localhost:8000` (whisper), `localhost:8765` (donsetch-http)
  without any extra DNS or links
- Easier to debug (`netstat`, `ss` on the host = what the bot sees)

**Cons:**
- Container is not isolated from the host network
- A port the bot would listen on (if it did) would take a host port
- You cannot run two bot instances with the same `BOT_TOKEN`

For a production-grade setup, switch to a bridge network + explicit
internal DNS names (`http://llama:8080/v1`). For a single standalone
machine, this is overkill.

## What `bot.py` deliberately does NOT do

- Does **not** run `Application.run_polling()` (see [CALL_LLAMA.md](CALL_LLAMA.md) §4).
- Does **not** keep any tool-calling state in llama-server's own
  MCP layer; the bot executes tools in its own process. This avoids
  the bug where llama-server loads the **list** of tools but
  sometimes fails to **execute** them (donsetch in particular never
  loaded correctly through the server-side MCP).
- Does **not** hardcode any model name in the code; if `MODEL` env
  is empty, the bot queries `/v1/models` and uses the first model
  it finds. See [CALL_LLAMA.md](CALL_LLAMA.md) §7.
- Does **not** log the live `req_body` dict — `json.dumps` with the
  default `ensure_ascii=True` mutates the dict in place. See
  [CALL_LLAMA.md](CALL_LLAMA.md) §6.
