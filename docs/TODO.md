# docs/TODO.md — LlamaBot operator & dev backlog

> This file is the canonical backlog for both the operator (real-world tests
> with a real Telegram user) and the developer (refactor cleanups). It's
> the first thing the next session should read.

---

## CRITICAL — operator tests pending (run on llmhost2 with real user)

These are the 6 manual end-to-end tests that need a real Telegram session
to exercise the v0.6.0 F3 module split + v0.6.1/v0.6.2/v0.6.3 hotfixes.

Each test was DESIGNED in the v0.6.0 review; none have been run yet.
Some require swapping Qwen3.8-27B-Ultra-Heretic-MTP-256k for Ornith
(temporarily) to test a behavior that Qwen doesn't reproduce. Swap
via docker-compose.yml `MODEL=` line + container restart, ~30s downtime.

### O1 — Stop button end-to-end UX (5 min, needs Ornith)

1. Set `MODEL=Ornith-X` in docker-compose.yml
2. `docker compose restart telegram-bot`
3. Send a long message that takes >10s to respond (Ornith is slow)
4. Click the "Stop" button while LLM is generating
5. Verify: response is truncated, [llm] prefix preserved, no
   duplicate thinking message, no error visible to user
6. Restore Qwen model in docker-compose.yml, restart

### O2 — Multi-user concurrency in group (10 min, needs Dmitri)

Two simultaneous users (you + Dmitri, id 117382588) send messages in
the test group. Both should get their own [llm] response with no
cross-contamination. The `v0.5.0` abort_event key bug is the regression
target; the v0.6.1 P0-1 + v0.6.3 T7 fixes should prevent it.

### O3 — Edit-during-LLM (10 min, needs Ornith + by-design limitation)

Send a long message, while LLM is responding EDIT the message to
something completely different. The response will be based on the
ORIGINAL message (this is by design — we use the user-text captured
at message-arrival, not the latest edit). Confirm: no error, the
edited text is what shows in Telegram's UI, the [llm] response
references the original. **Full fix is webhook migration (P1-ish
deferred to v0.7+).**

### O4 — /reset in forum topic (5 min)

Send a message in a forum topic, then `/reset`. The thread should
clear (no history loaded on next message). Verify the topic name
stays, the active thread pointer is reset, no error.

### O5 — Donsetch tool via real prompt (5 min)

Send a message that requires donsetch (e.g. "найди рецепт борща в инете").
The bot should call donsetch via the proxy, get a result, and reply.
Verify: tool call visible in logs, result in final reply, no
"tool not found" error.

### O6 — Visual stop-button feedback (2 min, needs Ornith)

1. Set Ornith model, restart
2. Send a long message
3. WHILE the thinking message is visible (the "thinking..." indicator),
   click Stop
4. Verify: the thinking message is immediately replaced by "[llm] ..."
   (or appropriate truncated text), not stuck at "thinking..."

---

## Active — current sprint (v0.6.3 done; v0.7 backlog)

### F4 - Bounded `_abort_events` cleanup - DONE in v0.6.0

**Status:** DONE in v0.6.0. See commits `6985249` (initial
implementation) and `935f4d2` (handlers.py migration), and v0.6.3
T1d (F4 LRU regression test added to in-tree selftest).
- `_abort_events` is `collections.OrderedDict` with
  `_ABORT_EVENTS_MAXSIZE = 200`.
- `_register_abort_event(chat_id, msg_id, ev)` does
  `move_to_end` on touch and `popitem(last=False)` to evict
  the oldest entry at cap.
- All 4 message handlers (photo, voice, document, text)
  use `_register_abort_event` since v0.6.1.
- In-tree selftest has F4 LRU regression test (3-line
  insert-250-assert-200) since v0.6.3.

### v0.6.3 NEW - 2 pre-existing selftest bugs exposed by T8 fix

**Status:** Deferred to v0.7 (uncovered by T8 rating parser fix).
**What broke:** Before v0.6.3, the rating parser test crashed with
`KeyError: 'rest'` and aborted the selftest mid-run. The
`_download_with_limit` and `sender-name flow` tests that come
AFTER it never executed. v0.6.3 T8 fixed the rating parser test
so the selftest now reaches these 2 tests, which are themselves
buggy.

- **`_download_with_limit` test (bot.py:1346-1418):** Fails with
  `AttributeError: '_FakeUpdate' object has no attribute 'effective_chat'`.
  The test creates `_FakeUpdate: pass` and calls
  `_b._download_with_limit(file, max, kind, _FakeUpdate())`. The
  function calls `_reply(update, ...)` which accesses
  `update.effective_chat` via `_is_group_chat(update)`. The test
  also patches `_b._reply = _fake_reply`, but the function uses
  `handlers._reply` (the local name in handlers.py namespace),
  not `bot._reply` — the F3 module split made them separate
  references. The test was broken silently since the F3 split
  in v0.6.0.
  - Fix: change test to use `_FakeUpdate` with `effective_chat`
    attribute, OR patch `handlers._reply` instead of `bot._reply`.

- **`sender-name flow` test (bot.py:1442-1480):** Fails with
  `KeyError: '_sender_name'`. The test reads
  `history[0]['_sender_name']` but the storage layer stores the
  key as something else (likely `sender_name` without underscore
  prefix, or in a different column). Pre-existing since v0.5.x.
  - Fix: grep what the storage layer actually returns and update
    the test's key, OR add `_sender_name` to the persist payload.

### P2 — v0.7 cleanups (deferred)

- **T12:** `RATING_EMOJI`/`BLOAT_EMOJI` duplicate between bot.py
  and prompts.py (one source of truth needed).
- **T13:** dispatch.py stale "Stage 6" comment at line ~62.
- **T14:** rating.py backward-compat shims (5 functions) — only
  `_parse_rating_response` is still used. Delete the rest.
- **T15:** state.py late import (the `from bot import ...` in
  _register_abort_event's imports — actually it doesn't, so
  this may be a false positive).
- **T16:** persistence.py import order: blank line between
  `from storage import get_store` and `from config import CONTEXT_MESSAGES`.
- **T17:** docstring at persistence.py:2 mentions
  "storage.store" which doesn't exist (should be "storage.get_store()").
- **T18:** ratings round-trip in selftest: confirm the rating
  column is `rating` (not `ratings`) — quick test
  consistency check.

### P3 — known limitations (v0.7+)

- **T19:** 6 operator tests O1-O6 (above).
- **T20:** Run in-tree selftest via `pytest` (add `pytest-asyncio`,
  wrap `_selftest()` as a test function). CI entry point.
- **T21:** Add `docs/REVIEW-MINIMAX-v6.md` archiving this review
  cycle.
- **T22:** Add `docs/REVIEW-MINIMAX-v7.md` (next review).
- **T23:** Webhook migration (Telegram long-polling → webhooks)
  to fix O3 edit-during-LLM properly.
- **T24:** donsetch_call re-export (currently skipped in selftest
  because the integration test imports it from `bot` but the
  F3 split moved it to call_llama).

---

## Done — v0.5.0 → v0.6.3

### v0.6.3 (commit 58b8be3)
- T1d: F4 LRU regression test ACTUALLY added to in-tree selftest.
- T7: existing abort_event test changed from raw insert to
  `_register_abort_event` (was the v0.6.0 P0-1 pattern).
- T8: rating parser test `rest` → `body` (KeyError since v0.6.0).
- T9: prompts.py duplicate `import re` consolidated.
- T10: handlers.py 5 unused call_llama imports trimmed.
- T11: handlers.py `is_group` dropped in 3/4 handlers.
- T6 (v0.6.0 P1): call_llama.py duplicate `_discover_default_model`
  removed (urllib version was shadowing the async httpx).
- T7 (v0.6.0 P1): MODEL discovery moved to `_ensure_model()`,
  called at top of `call_llama()` (not at import time). Bot
  now starts even if llama-server is down.
- T8 (v0.6.0 P1): 8× copy-pasted late imports trimmed to
  per-function minimal imports.
- CHANGELOG.md: v0.6.1 + v0.6.2 sections added (batched from
  the 2 silent hotfixes).
- docs/TODO.md: F4 marked DONE.

### v0.6.2 (commit 489841a)
- Re-export `bot.store` via `get_store()` (T1a).
- Re-export `_PER_USER_SEMAPHORE_LIMIT` and
  `_GLOBAL_LLM_SEM_LIMIT` (T1b).
- T2-T6: dead code cleanup (OrderedDict, storage import,
  DB_PATH, _apply_reaction, 3× duplicate caps, _s() wrapper).
- **Known gap:** T1d (LRU test) was claimed in commit message
  but not actually added. Fixed in v0.6.3.

### v0.6.1 (commit ea3d484)
- F4 LRU bound: handlers use `_register_abort_event` (P0-1).
- `_abort_events` moved to state.py, single source of truth (P0-2).
- bot.py no longer shadows config.py constants (P0-3).
- Single Storage instance via `get_store()` (P0-4).
- Dockerfile `COPY *.py ./` (P0-5).

### v0.6.0 (commit 4e24b2d)
- F3 module split: bot.py → 8 modules (config, prompts,
  persistence, rating, call_llama, handlers, dispatch, state).

### v0.5.2 (commit fbd9f42)
- F1 stale comment fix.
- F2 semaphore selftest added.

### v0.5.0
- Qwen migration, GROUP_CONTEXT, react_to_message.
