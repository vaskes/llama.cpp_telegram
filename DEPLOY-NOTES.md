# Deploy notes for v0.10.1 — 2026-10-12 morning

**v0.10.1 is the SINGLE bundle. It contains all fixes from v0.9.6,
v0.9.7, v0.10.0, and the Qwen wave-2 review. Just deploy v0.10.1
and you're done.**

## What v0.10.1 contains

**From v0.9.6 (T28 fix):** `parse_rating_response` maps unknown
types to "question" — fixes 2 pre-existing in-tree selftest
failures (P0-3 from Qwen review).

**From v0.10.0 (P0-3 from Qwen wave-1):** `prompts.py` now has
the LONG version of `GROUP_CONTEXT` (4786 chars vs old 2672).
Production LLM is no longer under-instructed. Dead code
(-291 lines) removed from `bot.py`. LRU eviction added for
`_user_semaphores` and `_bot_replies` (OrderedDict, maxsize=500).

**From v0.10.1 (Qwen wave-2 fixes):**
- 1-line `abort_event.is_set()` check before `send_reply()` in
  all 4 handlers (closes residual race window)
- Explicit `BEGIN`/`COMMIT` in `storage.delete_thread()` with
  `ROLLBACK` on exception (was relying on implicit transaction)
- 50-task backpressure cap on webhook queue (was: unbounded
  `asyncio.create_task` per incoming POST)
- Dead code in `dispatch.py` removed (F3 stage 5 shims, no
  longer needed)
- 3 prompt tests updated to check semantic markers, not length
- Test for LRU eviction at the actual 500 limit

## How to deploy

```bash
cd /opt/telegram-bot
sudo docker compose stop telegram-bot

# Copy the new code (4 files)
sudo cp /opt/llama.cpp_telegram/telegram-bot/handlers.py \
        /opt/telegram-bot/telegram-bot/handlers.py
sudo cp /opt/llama.cpp_telegram/telegram-bot/dispatch.py \
        /opt/telegram-bot/telegram-bot/dispatch.py
sudo cp /opt/llama.cpp_telegram/telegram-bot/storage.py \
        /opt/telegram-bot/telegram-bot/storage.py
sudo cp /opt/llama.cpp_telegram/telegram-bot/webhook.py \
        /opt/telegram-bot/telegram-bot/webhook.py
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

MEDIUM. The prompts change (long GROUP_CONTEXT) will visibly
change LLM behaviour in group mode (more [llm] tag adherence,
better rating scale, no tone policing). The behavioural change
is the INTENDED one. Watch for any 24h after deploy.

## Test status

```
$ cd /opt/llama.cpp_telegram && sudo python3 -m pytest tests/
... 57 passed in 0.50s
```

## What I did NOT do (by request)

- Did NOT copy any files to `/opt/telegram-bot/`
- Did NOT restart the bot
- Did NOT touch `/opt/whisper-api/`
- Did NOT change nginx config
- Did NOT rotate API_KEY / WEBHOOK_SECRET_TOKEN (still in old
  history cache; should rotate next time you have 5 min)

Production is still on v0.9.4 as of last check.
