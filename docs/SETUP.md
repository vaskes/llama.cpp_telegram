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
- `SEARXNG_URL` — if you have SearXNG up

## 5. Bring up dependencies

This repo does **not** run llama-server — that is your job.
At minimum, you need:

### Option A: you already have a llama-server
Do nothing. The bot will connect to `LLAMA_URL`.

### Option B: setting up from scratch on Radeon 780M
Follow the instructions in [vaskes/llama.cpp-rocm-780m](https://github.com/vaskes/llama.cpp-rocm-780m).

### Option C: for tool-calling (SearXNG)
Follow the instructions in [vaskes/llama.cpp_search](https://github.com/vaskes/llama.cpp_search)
— it includes a ready SearXNG + Playwright MCP, plus the `--mcp-servers-config` flag for llama-server.

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
- "what's new in AI" — if SearXNG is up, bot will call `searxng_search`
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
