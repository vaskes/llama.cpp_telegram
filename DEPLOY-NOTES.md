# Deploy notes for v0.9.6 / v0.9.7 — 2026-10-12 morning

Three v0.9.x releases in the worktree, not yet in production
(`/opt/telegram-bot/`). Each is a small, focused change.

## v0.9.6 — T28: parse_rating_response fall-back fix (PRODUCTION-READY)

**Commit:** `69ba8bb`
**Tag:** `v0.9.6`

### What changed

- `telegram-bot/rating.py` — `parse_rating_response` now maps any
  non-known type (including the literal `[[TYPE:unknown]]` from a
  misbehaving LLM) to `type="question"` and returns the whole
  input text as `body`. Previously, the function returned
  `type="unknown"` for both unprefixed text and the literal
  `[[TYPE:unknown]]` prefix.

### Why it matters

Two pre-existing in-tree selftest cases were failing on v0.9.4
production. The cases ("plain answer" and "[[TYPE:unknown]]")
were testing the SEMANTICALLY CORRECT behaviour ("treat unknown
as question"), but the parser was returning "unknown" as a
literal type, which is undocumented and was not handled by
`apply_rating_and_persist`. The fix makes the parser match
both the test expectations and the dispatcher's existing
fall-through behaviour (any non-(info|statement|bloat) type
already routes to the "persist and reply with full text" branch).

### How to deploy

1. In `/opt/telegram-bot/`, copy the new `rating.py`:
   ```bash
   cp /opt/llama.cpp_telegram/telegram-bot/rating.py \
      /opt/telegram-bot/telegram-bot/rating.py
   ```

2. Restart the bot:
   ```bash
   cd /opt/telegram-bot
   sudo docker compose restart telegram-bot
   ```

3. (Optional) verify the in-tree selftest is now 20/20:
   ```bash
   sudo docker exec telegram-bot env LLAMABOT_SELFTEST=1 \
     python3 -c "import bot; bot.run_self_test()"
   ```

   Look for `[OK] all 10 parse cases passed`. The other 19
   selftest groups should also print `[OK] ...`.

### Risk

LOW. The change is conservative: a more permissive fall-back
that matches the existing dispatcher behaviour. No code that
previously worked is broken. The 2 in-tree selftest failures
that were silently broken on v0.9.4 are now fixed.

---

## v0.9.7 — conftest.py hardening (TEST-ONLY)

**Commit:** `158bd21`
**Tag:** `v0.9.7`

### What changed

- `tests/conftest.py` — autouse fixture now also resets
  `state._user_semaphores` before AND after each test. Without
  this, a test that times out holding the sem leaves it locked
  for the next test.

### Why it matters

Defensive. Doesn't change production behaviour. Makes the
test suite more robust if a test times out.

### How to deploy

Not a production deploy. This file is in the worktree only.
The test suite can be re-run with `pytest tests/` and will
still pass.

### Risk

ZERO. Test infrastructure only.

---

## v0.9.5 — test suite polish (TEST-ONLY)

**Commit:** `658a4dc`
**Tag:** `v0.9.5`

### What changed

- 7 new tests (1 empty-URL auto-register, 3 _abortable_post
  race-condition, 3 storage known_topics round-trip).
- 36 → 43 tests, all in 0.48s.

### How to deploy

Not a production deploy. Test-only.

### Risk

ZERO. Tests only.

---

## Test status (all in worktree)

```
$ cd /opt/llama.cpp_telegram && sudo python3 -m pytest tests/
... 44 passed in 0.48s
```

---

## What I did NOT do (by request, you'll do it in the morning)

- Did NOT copy any files to `/opt/telegram-bot/`
- Did NOT restart the bot
- Did NOT touch `/opt/whisper-api/`
- Did NOT change nginx config

Production is still on v0.9.4. Deploy v0.9.6 first (the rating
fix is a real bug). v0.9.5 and v0.9.7 are test-only and
optional.
