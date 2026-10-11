# Deploy notes for v0.10.2 — 2026-10-12 morning

**v0.10.2 is the FINAL bundle. It contains all fixes from v0.9.6,
v0.9.7, v0.10.0, v0.10.1, AND v0.10.2. Just deploy v0.10.2 and
you're done.**

## What v0.10.2 adds (over v0.10.1)

**Two real blocking bugs caught by the wave-3 review that both
earlier waves missed:**

1. **`EMPTY_RESPONSE_FALLBACK` NameError** — `handlers.py:send_reply`
   used an undefined constant. Would have crashed the bot on any
   empty LLM response. Moved to `config.py`, added regression
   test.

2. **`_rate_limit_send` defined but never called** — full per-chat
   rate limiter was dead code. Bot unprotected from Telegram
   rate limit. Integrated into `send_reply`.

**Plus 6 non-blocking improvements:**

3. `_tag_sender` strips `_*` fields before LLM API (no internal
   marker leaks)
4. `transcribe_voice` language configurable via `WHISPER_LANGUAGE`
   env var (empty = autodetect)
5. `fetch_tools_from_llama` URL via `urlparse` (was: fragile
   `.replace('/v1', '')`)
6. `_check_user_slot` sends user message on queue timeout
   (was: silent drop)
7. `set_active_thread` wrapped in `BEGIN`/`COMMIT`
8. Webhook mode has SIGINT/SIGTERM signal handlers for graceful
   shutdown

## How to deploy

```bash
cd /opt/telegram-bot
sudo docker compose stop telegram-bot

# Copy the new code (5 files)
sudo cp /opt/llama.cpp_telegram/telegram-bot/handlers.py \
        /opt/telegram-bot/telegram-bot/handlers.py
sudo cp /opt/llama.cpp_telegram/telegram-bot/dispatch.py \
        /opt/telegram-bot/telegram-bot/dispatch.py
sudo cp /opt/llama.cpp_telegram/telegram-bot/storage.py \
        /opt/telegram-bot/telegram-bot/storage.py
sudo cp /opt/llama.cpp_telegram/telegram-bot/webhook.py \
        /opt/telegram-bot/telegram-bot/webhook.py
sudo cp /opt/llama.cpp_telegram/telegram-bot/call_llama.py \
        /opt/telegram-bot/telegram-bot/call_llama.py
sudo cp /opt/llama.cpp_telegram/telegram-bot/config.py \
        /opt/telegram-bot/telegram-bot/config.py
sudo cp /opt/llama.cpp_telegram/telegram-bot/rating.py \
        /opt/telegram-bot/telegram-bot/rating.py
sudo cp /opt/llama.cpp_telegram/telegram-bot/prompts.py \
        /opt/telegram-bot/telegram-bot/prompts.py
sudo cp /opt/llama.cpp_telegram/telegram-bot/state.py \
        /opt/telegram-bot/telegram-bot/state.py
sudo cp /opt/llama.cpp_telegram/telegram-bot/bot.py \
        /opt/telegram-bot/telegram-bot/bot.py

sudo docker compose up -d
```

Or simpler: rebuild the image.

## Risk

LOW-MEDIUM. The 2 blocking bugs would have crashed the bot on
specific inputs (empty LLM response, queue overflow). Both are
fixed and protected by regression tests. The non-blocking
changes are additive (rate limit is now active, more fields
filtered, etc.) — no behaviour regression for the happy path.

## Test status

```
$ cd /opt/llama.cpp_telegram && sudo python3 -m pytest tests/
... 60 passed in 0.51s
```

## What to monitor after deploy

- 24h: any `NameError` or `RetryAfter` in logs (the two blocking
  bugs are now fixed; if you see them post-deploy, that's a
  different bug)
- Group behaviour change: more `[llm]`-tagged replies (the
  longer prompt is now active)
- Rating scale: more accurate 1-10 distributions (the rules
  are no longer truncated)

## What I did NOT do (by request)

- Did NOT copy any files to `/opt/telegram-bot/`
- Did NOT restart the bot
- Did NOT touch `/opt/whisper-api/`
- Did NOT change nginx config
- Did NOT rotate API_KEY / WEBHOOK_SECRET_TOKEN

Production is still on v0.9.4 as of last check.
