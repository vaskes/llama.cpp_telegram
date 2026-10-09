# Setup — for humans

Step-by-step guide for a fresh Ubuntu 24.04 (or similar) install.
Assumes you work as a user with passwordless `sudo`.

## 0. Prerequisites

Check that you have:

```bash
docker --version          # Docker 24+
docker compose version    # v2 (compose is a subcommand, not a binary)
git --version
sudo -n true              # passwordless sudo (or replace with sudo -v)
```

If `docker compose` is not installed, install the plugin:
```bash
sudo apt-get install -y docker-compose-plugin
```

## 1. Clone

```bash
sudo mkdir -p /opt
sudo chown $USER:$USER /opt
git clone https://github.com/vaskes/llama.cpp_telegram.git
cd llama.cpp_telegram
```

## 2. Run install.sh

```bash
sudo ./scripts/install.sh
```

The script will:
- copy files to `/opt/telegram-bot/` and `/opt/whisper-api/`
- create `.env` from `.env.example` if it does not exist yet
- register systemd units `telegram-bot-compose.service` and `whisper-api-compose.service`
- enable them (but not start — configure `.env` first)

## 3. Find your Telegram user_id

Message any bot that can show your user_id (e.g. **@userinfobot**).
Copy the number.

## 4. Fill in `.env`

```bash
sudo $EDITOR /opt/telegram-bot/.env
```

Minimum required:
- `BOT_TOKEN` — from @BotFather (create a bot with `/newbot`)
- `ALLOWED_USER_IDS=YOUR_NUMBER` — otherwise the bot stays in LOCKDOWN and refuses everyone
- `LLAMA_URL=http://localhost:8080/v1` — address of your llama-server

Optional:
- `MODEL` — must match the `--alias` on the llama-server
- `ALLOWED_USERNAMES` — secondary auth by `@username` (case-insensitive)
- `DONSETCH_URL` — if you have [donsetch-http](https://github.com/dondai44423/donsetch)
  running (default `http://localhost:8765/mcp`)
- `CONVERSATIONS_DB` — path to the SQLite conversation store
  (default `/app/data/conversations.db`, bind-mounted from
  `./data` on the host by `docker-compose.yml`). The DB is
  created automatically on first bot start; conversations and
  sub-talks persist across `docker compose restart`.
- `CONTEXT_MESSAGES` — how many recent messages per sub-talk
  to send to the model (default 20). Older messages stay in
  the DB but are not in the model's context window.

## 5. Bring up dependencies

This repo does **not** run llama-server — that is your job.
At minimum, you need:

### Option A: you already have a llama-server
Do nothing. The bot will connect to `LLAMA_URL`.

### Option B: setting up from scratch on Radeon 780M
Follow the instructions in [vaskes/llama.cpp-rocm-780m](https://github.com/vaskes/llama.cpp-rocm-780m).

### Option C: for tool-calling (web search via donsetch)
Deploy [dondai44423/donsetch](https://github.com/dondai44423/donsetch)
on port 8765 (Rust MCP server with headless Chrome). Point the bot at it
by setting `DONSETCH_URL=http://localhost:8765/mcp` in `/opt/telegram-bot/.env`.
The bot will then have `web_search`, `web_fetch`, `web_crawl`, and
`web_screenshot` available.

The historical SearXNG-based stack in [vaskes/llama.cpp_search](https://github.com/vaskes/llama.cpp_search)
is deprecated — all major engines CAPTCHA-block Russian IPs. Use donsetch
instead.

## 6. Start the services

```bash
sudo systemctl start whisper-api-compose
sudo systemctl start telegram-bot-compose
sudo systemctl status telegram-bot-compose
sudo docker logs telegram-bot --tail 30
```

If you see `🤖 LlamaBot v2 (with tool-calling) started...` in the logs, the bot is alive.

## 7. Verify

Open Telegram, find your bot, send `/start`.
You should get the welcome text.

Then:
- "weather in Yalta" — bot will call `get_weather` and return the real temperature
- "what's new in AI" — if donsetch-http is up, bot will call `web_search`
- voice message — bot will transcribe via Whisper and reply to the text

## 8. Logs and updates

```bash
# logs
sudo docker logs -f telegram-bot
sudo docker logs -f whisper-api

# update bot to the latest version
cd llama.cpp_telegram
./scripts/update.sh
```

## 9. Backup before update

`scripts/update.sh` does NOT touch `.env`, but if you want to be safe:

```bash
sudo cp /opt/telegram-bot/.env /opt/telegram-bot/.env.bak
```

## What to do if the bot is silent

See [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

---

## Deployment mode A: Private chat (default)

The default mode. The bot serves one user in a 1:1 chat. All
sub-talks are stored in the local SQLite DB, identified by
`(user_id, sub_talk_name)`. Whitelist via `ALLOWED_USER_IDS` /
`ALLOWED_USERNAMES` is enforced.

This is the mode the bot has been running in since the beginning.
The setup steps above are for this mode.

---

## Deployment mode B: Group with Telegram Topics (multi-user)

In this mode the bot is a member of a Telegram supergroup with
**Topics** enabled. Each topic is an independent conversation
stream; the bot reads `message_thread_id` from every incoming
message to know which topic (and therefore which history) to use.

### B.1 — Create the supergroup

In Telegram:
1. Create a new group.
2. **Group Settings → Topics → Enable** (you must convert it to a
   supergroup first; this happens automatically when you enable
   Topics, or via `Group Info → Edit → Chat Type → Public/Private`).

If Topics cannot be enabled, the bot will not be able to create
topics and `/newsub` will fail with `400 Bad Request: topics must
be first enabled`.

### B.2 — Add the bot to the group and promote it

1. Add the bot by `@username`.
2. Open the member list, long-press the bot → **Promote to Admin**.
3. Enable **Manage Topics**. The other admin permissions are not
   required for normal operation.

Without `Manage Topics`, `createForumTopic` and `deleteForumTopic`
will return 403.

### B.3 — Configure the bot

In `/opt/telegram-bot/.env`, **do not** put anything in
`ALLOWED_USER_IDS` for the people who should be able to use the
bot — group mode skips the whitelist check entirely. (You can
still set the whitelist for a private-chat fallback, but
group members do not need to be in it.)

```bash
BOT_TOKEN=...
LLAMA_URL=http://localhost:8080/v1
# Leave ALLOWED_USER_IDS empty in group-only deployments.
# In mixed deployments (private + group), list private users here.
```

### B.4 — Start the bot

```bash
sudo systemctl restart telegram-bot-compose
sudo docker logs telegram-bot --tail 30
```

You should see `is_forum=True` in the log when a topic message
arrives, and `chat_type=supergroup` for any group message.

### B.5 — Try it

In the General topic of the group:
1. `/newsub research` — a new topic "research" appears in the
   sidebar. Switch to it.
2. Send "what's the capital of France?" — the bot replies
   **inside the "research" topic**, not in General.
3. Send another question — the bot's context is per-topic.
   The "research" topic remembers your first question; a separate
   "main" topic does not.
4. `/subs` — list all topics. No inline keyboard (Telegram's
   sidebar is the switcher).
5. `/delsub research` — the topic is removed from Telegram and
   its message history is wiped from the local DB.

### B.5b — Noise filter (the "honor system" for LLMs)

The bot stays silent for messages that are either
  - from another Telegram bot (`is_bot = true`), or
  - containing the substring `[llm]` (case-insensitive) anywhere
    in the text or caption.

The `[llm]` convention is set in the pinned welcome message of
the group. It means "I am an active LLM participant — please
yield the floor and don't double-answer me". In private mode
the filter does not apply (there is only one human; no one to
yield to).

This is an honor system. The bot cannot detect whether a
particular LLM is actually being cooperative. If the group
gets noisy (spammers, silent harvesters), the operator will
need to add moderation features (`/ban`, `/del`). Tracked in
TODOS.

### B.6 — How routing works

| Mode | Trigger | `chat_id` | `thread_id` | Whitelist |
|------|---------|-----------|-------------|-----------|
| Private | `chat.type == "private"` and `is_forum` is False | `user_id` | active sub-talk name from DB | enforced |
| Group General | `chat.is_forum == true` and `message_thread_id is None` | `chat_id` (negative) | `"general"` (sentinel) | skipped |
| Group Topic | `chat.is_forum == true` and `message_thread_id` is set | `chat_id` (negative) | `str(message_thread_id)` | skipped |

The bot decides which mode a message belongs to by inspecting
`chat.is_forum` and `message_thread_id`. The same SQLite store
serves both modes — keys are `(chat_id, thread_id)` either way.

### B.7 — Security model for group mode

Group mode **does not** check the `ALLOWED_USER_IDS` whitelist.
Any member of the supergroup can use the bot. Access control is
delegated to Telegram itself (group membership, topic-specific
permissions, admin status). If you need finer-grained control,
lock the group to invite-only and add users manually.

See [SECURITY.md](SECURITY.md) §"Group mode security" for the
full model.

### B.8 — Limitations

- `/sub <name>` in group mode is informational only — you cannot
  "navigate" to a topic programmatically; users tap topics in
  the sidebar.
- Bot commands in a topic only affect that topic (and the bot
  replies in the same topic via `_reply`'s `message_thread_id`
  pass-through).
- `getForumTopics` is a generator with a 1-second hard limit per
  call; with thousands of topics the first call may need retries.
  The retry loop in `cmd_subs` / `cmd_delsub` handles this.
- Mixed private+group deployments: a user in both `ALLOWED_USER_IDS`
  AND a group with the bot will be served by the private mode
  if they message the bot directly, and by group mode if they
  post in a group topic. Their conversations are isolated.
