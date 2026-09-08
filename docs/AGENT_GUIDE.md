# Agent Guide — for AI agents

Short reference for operating this repo. If you are an AI agent and someone
asked you to do something with the bot, start from this file.

## Where things live

```
/opt/telegram-bot/          ← live copy (created by install.sh)
├── bot.py                  ← all bot code
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

### Check SearXNG
```bash
curl -s -m 5 "http://localhost:8888/search?q=test&format=json" | head -c 200
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
```bash
cd /path/to/llama.cpp_telegram
./scripts/update.sh
```

## What you MUST NOT do

1. ❌ Commit `.env` — it has tokens. Run `git status` before `git add`.
2. ❌ Change `network_mode: "host"` without understanding the implications — it punches through to the host network.
3. ❌ Enable `parallel_tool_calls: true` without testing — the current implementation assumes sequential calls.
4. ❌ Hardcode real credentials in `docker-compose.yml` — only env vars.
5. ❌ Remove `DISABLED_TOOLS` from `bot.py` without a re-audit. `read_file`,
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

- `bot.py` has hardcoded `192.168.10.7:8080` references in comments, but
  in code it uses env vars `LLAMA_URL` and `WHISPER_URL`. Intentional.
- SearXNG engines on cloud IPs often CAPTCHA-out. `get_weather` via wttr.in
  always works. If a user asks "why is Google not searching" — this is
  a [known issue](https://github.com/searxng/searxng/issues) with
  user-agent detection, not a code bug.
- Docker `network_mode: host` means the bot has no IP of its own —
  `0.0.0.0` bindings in the container occupy host ports. If something
  starts conflicting, switch to a bridge network + explicit ports.
