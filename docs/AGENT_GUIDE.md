# Agent Guide — for AI agents

Short reference for operating this repo. If you are an AI agent and someone
asked you to do something with the bot, start from this file.

## Where things live

```
/opt/telegram-bot/          ← live copy (created by install.sh)
├── bot.py                  ← public facade: re-exports the module surface
├── dispatch*.py            ← routing, polling, webhook, group, edit paths
├── handlers.py             ← handle_text/photo/voice/document
├── call_llama.py + llm_*.py ← the turn and its leaf modules
├── Dockerfile              ← python:3.11-slim + pip install
├── docker-compose.yml      ← network_mode: host
├── .env                    ← credentials (DO NOT commit)
├── requirements.txt        ← python-telegram-bot, httpx
└── files/                  ← static assets (currently empty)

/opt/whisper-api/
├── docker-compose.yml      ← fedirz/faster-whisper-server
└── cache/                  ← downloaded models

/etc/systemd/system/
├── telegram-bot-compose.service
└── whisper-api-compose.service
```

`bot.py` is a **facade**, not the implementation — it re-exports the
module surface so that `from bot import call_llama` keeps working for
forks and downstream scripts. The code lives in the 53 modules beside it;
`TECH-DEBT.md` item 1 records what that cost and why it was done.

The live deployment on the operator's host uses a different layout
(`/opt/telegrambot/`), and it keeps **no git of its own**: the image
arrives from a release. `scripts/install.sh` is the source of truth for a
fresh install, and `BOT_DIR` overrides it.

### Before you change anything

Read [`CODEBOOK.md`](CODEBOOK.md) first. It is the set of rules this
repository learned by breaking them: what a monolith is here, what a
tripwire has to be able to do, and which of the two delivery paths a
feature has to live on for it to run in production.

Two of those rules are enforced mechanically, and both will fail your test
run rather than your build:

- `tests/test_size_budget.py` — a function over 200 lines, or a module
  over 1000 lines that is not in the debt register, fails the suite.
- `tests/test_no_model_coauthor_trailers.py` — a co-author trailer
  naming a model fails the suite.

Section 5 of the codebook says what a new check has to prove before it
counts as one.

## Common operations

### Check the bot is alive
```bash
sudo docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" | grep -E "(telegram|whisper|llama)"
sudo docker logs telegram-bot --tail 20
sudo docker logs whisper-api --tail 20
```

### Check llama-server
```bash
curl -s -m 5 http://localhost:8080/health        # → {"status":"ok"}
curl -s -m 5 http://localhost:8080/v1/models | head -c 400
```

### Check donsetch-http
```bash
curl -s -m 5 http://localhost:8765/health
# or list tools (depends on donsetch version)
```

### Restart the bot (e.g. after editing bot.py)
```bash
cd /opt/telegram-bot
sudo docker compose build telegram-bot
sudo docker compose up -d --no-deps --force-recreate telegram-bot
sudo docker logs telegram-bot --tail 10
```

### Add a new user to the whitelist
```bash
sudo $EDITOR /opt/telegram-bot/.env
# → add the user_id to ALLOWED_USER_IDS=comma_separated
sudo systemctl restart telegram-bot-compose
```

### Find a user's Telegram id
- ask them to send `/start` to **our** bot
- the bot will reject them (LOCKDOWN if user_id is not in whitelist), but in the logs:
  ```
  [SECURITY] rejected id=XXXXXXXXX @username msg='/start'
  ```
- or ask them to send `/start` to **@userinfobot** in Telegram

### See who the bot has rejected (security log)
```bash
sudo docker logs telegram-bot 2>&1 | grep "SECURITY" | tail -30
```

### Update to the latest version

Two paths, and which one you use depends on what the host has.

**A host you keep a clone on** (the quickstart layout):
```bash
cd /path/to/llama.cpp_telegram
git fetch --tags && git checkout <tag>
./scripts/update.sh
```

**The production host** — it keeps **no git**, and the release is the
deployable unit:

1. push the tag (`vX.Y.Z-stable`): the CI builds the image from the
   tagged commit and attaches it, with the files the deploy needs, to
   the GitHub Release. The workflow holds no secret and has no way to
   reach any host;
2. run the host's own deploy script with that tag. The script is the
   host's (it holds the compose file, the `.env` and the volume), it is
   the same script a hand deploy runs, and it judges itself: the health
   endpoint, the restart count, the smoke test **before and after**
   (a check that was already red is not this deploy's failure), and a
   rollback to the image that was running before if anything fails.

Do **not** open the production host to a CI runner to do this. The
runners' addresses are 7081 CIDR — about 28.6 million of them — and the
host is behind the operator's router; the access that already exists is
the door. See [docs/ARCHITECTURE.md](ARCHITECTURE.md#the-release-workflow)
and CODEBOOK rule **X-18**.

## What you MUST NOT do

1. ❌ Commit `.env` — it has tokens. Run `git status` before `git add`.
2. ❌ Change `network_mode: "host"` without understanding the implications — it punches through to the host network.
3. ❌ Enable `parallel_tool_calls: true` without testing — the current implementation assumes sequential calls.
4. ❌ Hardcode real credentials in `docker-compose.yml` — only env vars.
5. ❌ Remove `DISABLED_TOOLS` from `config.py` without a re-audit. `read_file`,
   `write_file`, `exec_shell_command` are disabled for a reason — it means
   **any** whitelisted user could run arbitrary shell on the host via tool-calling.
   Do NOT enable them.

## If a user complains the bot "doesn't reply"

Run this checklist first:

```bash
# 1) is the bot running at all?
sudo docker ps | grep telegram

# 2) is the container fresh? what is in the logs?
sudo docker logs telegram-bot --tail 50 | tail -30

# 3) is it in LOCKDOWN?
sudo docker logs telegram-bot --tail 200 | grep -E "(LOCKDOWN|whitelist)"

# 4) any rejections for this user?
sudo docker logs telegram-bot --tail 200 | grep "rejected id=THAT_SAME_ID"

# 5) is the llama-server alive?
curl -s -m 5 http://localhost:8080/health
```

If `LOCKDOWN` — that user is not in the whitelist. Check `ALLOWED_USER_IDS` in `.env`.

If `rejected id=...` — the bot knows them but does not allow them. Add to whitelist.

If the llama-server does not respond — that is a **separate task**, not related to this repo.

## Known tech debt

- `bot.py` had hardcoded LAN-IP-and-port references in comments
  (e.g. `<lan-ip>:<llama-port>`). These were scrubbed in 2026-10-13
  because real LAN IPs leaked the internal topology of the
  deployment into a public GitHub repo. The runtime code
  already uses env vars `LLAMA_URL` and `WHISPER_URL`; only the
  comments needed scrubbing. Intentional.
- Donsetch-http aggregates Bing, DDG, Mojeek, Yahoo, Brave, Google via
  Playwright headless Chrome, so CAPTCHA-blocked engines are bypassed.
- Docker `network_mode: host` means the bot has no IP of its own —
  `0.0.0.0` bindings in the container occupy host ports. If something
  starts conflicting, switch to a bridge network + explicit ports.

## Group mode quick reference

If the bot is a member of a Telegram supergroup with Topics:

```
is_forum=True  in the logs          → group mode is detected
message_thread_id=<num>              → message is in a specific topic
message_thread_id=None               → message is in the General topic
```

Bot commands in group mode:
  - `/newsub <name>` — creates a Telegram forum topic
  - `/subs` — lists topics the bot created (from local DB)
  - `/delsub <name|id>` — deletes a topic
  - `/reset` — clears the LLM's memory in the current topic
  - `/here` — shows current topic + last message
  - `/stats` — group-level stats (topic count, msg totals)

Noise filter (group mode only):
  - The bot does NOT reply to messages from other Telegram bots
  - The bot does NOT reply to messages containing `[llm]`
    (case-insensitive) — the convention for active LLM
    participants per the group's pinned welcome message
  - Suppressions are logged as
    `[dispatch] muting group-mode message from <who>`
  - The filter is applied at the dispatch layer
    (`_dispatch_update`), so it covers message AND command
    handlers uniformly
  - In private mode the `[llm]` filter is a no-op (no other
    LLM to yield to in a 1:1 chat)

Whitelist: in group mode the `ALLOWED_USER_IDS` env var is
**skipped entirely** -- for the conversation path only. Slash
commands and the inline-keyboard callbacks are still checked against
`ALLOWED_USER_IDS`, in a group as well as in private (since
2026-10-22). Access control for the conversation is delegated to Telegram
(group membership + per-topic permissions). For details, see
[docs/SECURITY.md](SECURITY.md) §"Group mode security".

The bot's SQLite store at `./data/conversations.db` (or
`/app/data/conversations.db` in the container) has three tables:
  - `chat_threads(chat_id, thread_id, ...)` — known threads
  - `messages(id, chat_id, thread_id, role, content, ...)` — history
  - `active_thread(chat_id, thread_id)` — private mode only
  - `known_topics(chat_id, message_thread_id, name, ...)` — group mode

Inspect / repair:
  ```bash
  docker exec telegram-bot sqlite3 /app/data/conversations.db \
      "SELECT * FROM known_topics"
  docker exec telegram-bot sqlite3 /app/data/conversations.db \
      "DELETE FROM known_topics WHERE message_thread_id = <id>"
  ```
