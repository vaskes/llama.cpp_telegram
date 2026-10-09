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
