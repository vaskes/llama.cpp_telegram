# Deploy notes for v0.9.6 / v0.9.7 / v0.10.0 — 2026-10-12 morning

Three releases in the worktree, not yet in production
(`/opt/telegram-bot/`). Each is a focused, low-risk change.

## v0.10.0 — P0-3 prompts drift + LRU hardening (HIGHEST PRIORITY)

**Commit:** `4611de4`
**Tag:** `v0.10.0`

### What changed

- `telegram-bot/prompts.py` — Now has the LONG version of
  `GROUP_CONTEXT` (4786 chars vs old 2672), `RATING_RULES` (2463
  chars vs old 865), and `WELCOME_TEXT` (934 chars vs old 316).
  The short version was missing critical rules: the [llm] tag
  requirement (one bullet, not a section), the rating scale
  with concrete examples, the react_to_message tool description,
  the "do not police tone" rule, the "no auto-bans" rule.
- `telegram-bot/bot.py` — Removed the dead duplicates
  (-291 lines, -11%). Also removed `RATING_MODE: bool = ...`,
  `_RATING_PREFIX_RE`, `_EMOJI_CHAR_RE`, `_SUBTALK_NAME_RE`,
  `_TOPIC_NAME_RE`, `_LLM_TOKEN_RE` — all now imported from
  prompts.py.
- `telegram-bot/state.py` — `_user_semaphores` is now
  `OrderedDict` with LRU at `_USER_SEMAPHORES_MAXSIZE=500`.
  `_bot_replies` is now `OrderedDict` with LRU at
  `_BOT_REPLIES_MAXSIZE=500`. New helpers `_register_bot_reply()`
  and `_pop_bot_reply()`.
- `telegram-bot/handlers.py` — `send_reply` now uses
  `_register_bot_reply()` (was: direct dict assignment).
- `tests/test_state.py` — 8 new tests:
  - 3 for `_user_semaphores` LRU (eviction, touch, under-limit)
  - 2 for `_bot_replies` LRU (eviction, pop)
  - 3 REGRESSION tests for P0-3 (prompts has long version)

### Why it matters (P0-3 is the real sleeper bug)

For weeks, the LLM in production was getting the SHORT version
of `GROUP_CONTEXT` (~30% shorter than intended). This means:
- The LLM was less likely to add the [llm] tag → peer LLMs
  rated its messages as if they were human
- The rating scale lacked concrete examples → more random
  ratings
- The "no auto-bans" and "no tone policing" rules were
  weaker → more policing-style refusals

Operators may have noticed weaker adherence to group rules
over the past weeks and attributed it to model drift. The
real cause was a shorter prompt.

### How to deploy

```bash
# Stop the bot
cd /opt/telegram-bot && sudo docker compose stop telegram-bot

# Copy the new code
sudo cp /opt/llama.cpp_telegram/telegram-bot/prompts.py \
        /opt/telegram-bot/telegram-bot/prompts.py
sudo cp /opt/llama.cpp_telegram/telegram-bot/bot.py \
        /opt/telegram-bot/telegram-bot/bot.py
sudo cp /opt/llama.cpp_telegram/telegram-bot/state.py \
        /opt/telegram-bot/telegram-bot/state.py
sudo cp /opt/llama.cpp_telegram/telegram-bot/handlers.py \
        /opt/telegram-bot/telegram-bot/handlers.py

# Restart
sudo docker compose up -d
```

Or simpler: rebuild the image.

### Risk

MEDIUM. The biggest code change in the v0.9.x cycle. The
prompt content changes ARE user-visible (LLM will behave
differently in groups). But:
- The longer prompt is the INTENDED one
- The change is additive (more rules, not fewer)
- The new content was already in the codebase (just dead)
- 52/52 tests pass, including regression tests for prompt content

If you want to be cautious: deploy v0.9.6 first (the
rating.py fix is conservative and isolated), then v0.10.0
separately. But the prompts drift is the more impactful bug
to fix sooner.

---

## v0.9.6 — T28: parse_rating_response fall-back fix

**Commit:** `69ba8bb`
**Tag:** `v0.9.6`

### What changed

- `telegram-bot/rating.py` — `parse_rating_response` now maps
  any non-known type (including the literal `[[TYPE:unknown]]`)
  to `type="question"` and returns the whole input text as
  `body`.

### How to deploy

```bash
cd /opt/telegram-bot && sudo docker compose stop telegram-bot
sudo cp /opt/llama.cpp_telegram/telegram-bot/rating.py \
        /opt/telegram-bot/telegram-bot/rating.py
sudo docker compose up -d
```

### Risk

LOW. Conservative fall-back. No code that previously worked
is broken.

---

## v0.9.7 — conftest.py hardening (TEST-ONLY)

**Commit:** `158bd21`
**Tag:** `v0.9.7`

### What changed

- `tests/conftest.py` — autouse fixture now also resets
  `state._user_semaphores` before AND after each test.

### How to deploy

Not a production deploy. Test infrastructure only.

### Risk

ZERO.

---

## Test status (all in worktree)

```
$ cd /opt/llama.cpp_telegram && sudo python3 -m pytest tests/
... 52 passed in 0.49s
```

---

## Deployment order (recommended)

1. **v0.9.6 first** (rating.py only, low risk)
2. **v0.10.0 second** (prompts + state + handlers + bot.py,
   medium risk but the prompts drift is the bigger bug)
3. **v0.9.7 last** (test-only, no production deploy)

Or deploy v0.10.0 directly. The prompts drift is the
most important fix.

## What I did NOT do (by request)

- Did NOT copy any files to `/opt/telegram-bot/`
- Did NOT restart the bot
- Did NOT touch `/opt/whisper-api/`
- Did NOT change nginx config

Production is still on v0.9.4 as of last check.
