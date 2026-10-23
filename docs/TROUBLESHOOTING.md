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

## donsetch-http is not responding

```bash
curl -s -m 5 http://localhost:8765/health
sudo -n docker logs donsetch-http --tail 20
```

Donsetch routes searches through real Chromium via Playwright. Common issues:
- Donsetch container not running: `sudo -n docker ps | grep donsetch`
- Chromium not installed inside the container (rebuild with Playwright deps)
- `DONSETCH_URL` in bot's `.env` is wrong (must end in `/mcp`,
  e.g. `http://localhost:8765/mcp`)

The bot falls back to plain `reply` if donsetch is unreachable, so users
still get answers to non-search questions.

## Whisper does not recognize voice

```bash
sudo docker logs whisper-api --tail 20
```

- Model is still downloading — first start pulls 1.5 GB, ~5-10 min
- Voice is too quiet / noisy — faster-whisper handles it poorly
- Language — `WHISPER_LANGUAGE` defaults to `ru` (in
  `telegram-bot/config.py`). If the voice is in English, recognition will
  be off. Set the env var to empty for autodetect, or to the right code
- Bot is on `lan_macvlan` but whisper is on the host network — the
  macvlan sub-interface cannot ARP its parent host, so the bot
  never reaches whisper. See "Networking between containers does
  not work" above for the macvlan fix.

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

The bot is one Docker container. It needs to reach three other
endpoints: the LLM, Whisper (voice), and donsetch-http (web
tools). Which network mode you use depends on how those
services are deployed.

### Single host (simplest)

All services on the same host, all on host network. The bot
container uses network_mode: host and reaches everything
via localhost:

  network_mode: host
  environment:
    LLAMA_URL=http://localhost:8080/v1
    WHISPER_URL=http://localhost:8000
    DONSETCH_URL=http://localhost:8765/mcp

This is the path of least surprise for development. It is
NOT compatible with webhook mode if you want a reverse
proxy on a different container -- see below.

### Network layout (since 2026-10-13)

**Current layout:** the bot and whisper-api both run with
`network_mode: host`. No Docker bridge network, no static
container IPs, no macvlan. The bot binds to `0.0.0.0:8443`
on the host. The reverse proxy (nginx, also on host) proxies
HTTPS 443 -> aiohttp 8443 via `127.0.0.1` (or via the
host LAN IP if the proxy is on a different host).

**Why we dropped macvlan:** macvlan L2 isolation made the
bot unable to reach services on the same host (whisper,
the bash-sandbox side-car, and even llama-server when
bound to the host main IP) without elaborate PBR/DNAT
routing on the router. The macvlan sub-interface cannot
ARP the parent host, so any service on the host main IP
was unreachable from the bot container.

**For your reverse proxy:** point it at `127.0.0.1:8443`
if it runs on the same host, or at `<host-lan-ip>:8443` if
it runs on a different host on the same LAN. Both work
without any special routing.

**Historical (pre-2026-10-13):** before the host-network
switch, the bot was on a `lan_macvlan` Docker network with
a static IP (192.168.10.5). The reverse proxy reached it at
that IP. This worked when all other services were also on
the same macvlan, but broke the moment any service moved
to the host. See git history if you need to roll back; do
not start fresh with macvlan. See [docs/WEBHOOK.md](WEBHOOK.md)
section 4.3.1 for the production layout note.

**Symptom of a misconfigured network:** bot logs show
`httpx.ConnectError: All connection attempts failed` on
a target that is running. Fix by checking that:

1. The target service is bound to `0.0.0.0`, not just
   `127.0.0.1` or a specific interface IP.
2. The host firewall allows traffic on the target port
   from the host itself (loopback is usually open, but
   if you are reaching a remote host, the remote firewall
   must allow it).
3. If the target is on a different host, the URL in
   `.env` is the LAN IP of that host, not `localhost`.

### Verify connectivity

From the bot host (or any host on the same LAN):

  for name, host, port in [("llama", "<llama-host-lan-ip>", 8080),
                            ("whisper", "<whisper-host-lan-ip>", 8000),
                            ("donsetch", "<donsetch-host-lan-ip>", 8765),
                            ("sandbox", "<sandbox-host-lan-ip>", 8090)]:
      if timeout 3 bash -c "echo > /dev/tcp/$host/$port" 2>/dev/null; then
          echo "$name OK"
      else
          echo "$name FAIL"
      fi

For an in-container test (use `network_mode: host` so the
container sees the host network namespace; otherwise exec
into a side-car container that does):

  docker exec telegram-bot python3 -c "
  import socket
  for name, host, port in [("llama","<llama-host-lan-ip>",8080),
                            ("whisper","<whisper-host-lan-ip>",8000),
                            ("donsetch","<donsetch-host-lan-ip>",8765),
                            ("sandbox","<sandbox-host-lan-ip>",8090)]:
      try:
          s = socket.create_connection((host, port), timeout=3); s.close()
          print(f"{name} OK")
      except Exception as e:
          print(f"{name} FAIL: {e}")
  "



## System limits

If the bot stops replying after N messages:

```bash
ulimit -n          # fd limit
df -h /opt         # disk
free -h            # memory
```

A llama-server with a large model and 8K context can eat all RAM. Watch
`MEM%` in `docker stats`.

---

## Group mode (Telegram Topics) issues

### `/newsub <name>` says "Created sub-talk" instead of "Created topic"

This is a **stale toast from a prior bot version** (before commit
`326ca48`). The current code branches on `chat.is_forum` and creates a
real Telegram topic in group mode. The old "Created sub-talk" string
came from the private-mode fallback that was incorrectly triggered
when `message_thread_id is None` (which is the case for the General
topic of every forum-enabled supergroup).

To clean up the chat: long-press the old bot message → Delete.

### `/newsub Болталка` rejected with "Name rules..."

Old bot version (pre-`326ca48`). Names like Cyrillic / emoji are
allowed by the current `^[^\s]{1,128}$` regex. Update the bot
(`cd llama.cpp_telegram && git pull && sudo ./scripts/update.sh`).

### `/delsub <name>` returns "❌ No topic named ..."

The `known_topics` table only has topics **created via `/newsub`**.
Topics created manually in the Telegram UI, or by another bot,
are not in the table.

Options:
  - Use the Telegram UI to delete: open the topic → tap the topic
    name in the header → "Delete topic".
  - Re-create via `/newsub <same name>`. The new topic will be
    tracked. Then delete the old one via UI.
  - Try `/delsub <numeric_id>` if you happen to know the
    `message_thread_id` (visible in the URL of a topic in
    Telegram web).

### Bot in group, but `/subs` shows nothing even after `/newsub X`

Two possibilities:

  - The container was restarted AFTER the topic was created
    but BEFORE commit `0dce580`. Pre-`0dce580` had no
    `known_topics` table; topics created then were not
    persisted. Solution: delete the topic via UI, recreate
    via `/newsub`.

  - The `data/conversations.db` bind-mount on the host is
    wrong (the bot's data dir is not actually bind-mounted
    from the host). Verify:
    ```bash
    docker exec telegram-bot ls -la /app/data/
    # → conversations.db, etc.
    ```
    If empty: check `docker-compose.yml` for the
    `./data:/app/data` line under the bot service.

### Bot crashes on every command with `AttributeError: 'NoneType'`

Old bot version (pre-`cf96920`). Triggered when the user edited
a command message (Telegram sends an `edited_message` update
where `update.message is None`). The current code branches
in the polling loop: edited commands are normalised into a
fresh `message` and re-processed; edited text is dropped.

### `is_forum=True` in the log but bot still uses private-mode strings

`is_forum` is the **chat** attribute (set by Telegram when the
group is a forum-enabled supergroup). It does NOT change per
message. If you see `is_forum=True` but the bot says "Created
sub-talk", the bot is running an old build. Pull, rebuild, restart:

```bash
cd /opt/llama.cpp_telegram
git pull
sudo ./scripts/update.sh
```

### Topic `delete_forum_topic` returns 400 "Topic not found"

You tried to delete a topic that the bot never created (or that
was already deleted). The `known_topics` table still has the row;
clean it up by deleting the row directly:

```bash
docker exec telegram-bot sqlite3 /app/data/conversations.db \
    "DELETE FROM known_topics WHERE message_thread_id = <id>"
```

### Topic `create_forum_topic` returns 400 "Topics must be first enabled"

The group is not (yet) a forum-enabled supergroup. Settings:
1. Convert group to supergroup if it's still a basic group
   (Group Info → Edit → "Chat Type" → Public/Private)
2. Settings → Topics → Enable
3. Re-promote the bot to admin with "Manage Topics"

### Bot cannot delete a message: "message can't be deleted"

The bot can only delete messages that are < 48 hours old
(Telegram's limit on `deleteMessage` for non-service messages).
For older messages: delete via UI.

### Bot doesn't reply to my message in a group

The group-mode noise filter suppresses the bot's reply when
the message is from another Telegram bot OR contains `[llm]`
anywhere in the text or caption. The pinned welcome message
in the group uses `[llm]` as the convention for active LLM
participants — they are expected to mark their replies, and
the bot stays silent on marked messages to avoid
double-answering.

If you (a human) typed `[llm]` by accident, just send a new
message without the marker.

The dispatch path logs every suppression:
```
[dispatch] muting group-mode message from @<username>
```
or
```
[dispatch] muting group-mode message from id=<n>
```
so you can verify the filter is doing what you expect.

In private mode the `[llm]` filter is disabled — there is
no other LLM to yield to, so typing `[llm]` in a private
chat does not affect the bot.
