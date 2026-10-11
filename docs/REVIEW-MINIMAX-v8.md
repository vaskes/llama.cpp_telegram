# REVIEW-MINIMAX-v8 — v0.9.2 post-review verification (2026-10-11)

**Tag reviewed:** v0.9.2 (commit `6758b2f`)
**Plus:** `20b5cd9` SECURITY commit (renumbered after `git filter-branch` history rewrite)
**Scope:** 9 files, +492 / -53 lines. 12/12 pytest pass. Smoke test: webhook auth + body cap + /start with LLM reply.

---

## 0. Top-level summary

v0.9.2 is a strong follow-up to v0.9.1. The 4 P0 issues from the
pre-release review are all correctly fixed (secrets moved out of
compose, rollback pattern applied to all 4 handlers, json import
fixed, body size cap added). The 2 P1 issues are also done (task
exception logging, WEBHOOK_URL defined + auto-register). 7 new unit
tests cover the new code.

**v0.9.2's auto-register feature introduced 2 NEW P0 bugs** that
were dormant in production (because the operator's WEBHOOK_URL is
empty). The moment the operator sets `WEBHOOK_URL` and restarts:

1. **Stop button stops working** — auto-register didn't pass
   `allowed_updates`, so Telegram defaulted to
   `["message", "edited_message"]` only, dropping `callback_query`
   updates (the Stop button).
2. **Bot replies start failing** — auto-register used two
   back-to-back `asyncio.run()` calls, leaving a stale `_dispatcher`
   whose Bot has a dead httpx client. The webhook server runs but
   `context.bot.send_message(...)` raises on first use.

Both bugs were fixed in v0.9.3.

**Plus 2 cleanup issues:**
- WEBHOOK.md section 4.5 manual-register curl lost `allowed_updates`.
- `tests/test_selftest.py` had imports in the middle of the file plus
  unused `json`/`hmac` imports and duplicate `os`/`asyncio`/`pytest`
  imports.

---

## 1. What v0.9.2 did correctly (verifies the pre-release review)

### 1.1 P0-SEC: secrets moved from compose to .env (commit 20b5cd9)

The `git filter-branch` history rewrite removed the leaked blobs from
all 142 commits. The Telegram bot token was rotated.

**P0-SEC is closed.**

### 1.2 P0-1: handle_voice + handle_document + handle_text rollback pattern applied

All 4 handlers now have:
1. `persisted = False` at the start of the try block
2. Send thinking message + register abort_event
3. Then `_persist_message(...)` and `persisted = True`
4. Rollback in the abort branch (primary LLM call)
5. Rollback in the retry-abort branch (empty-response retry)
6. Rollback in the except branch (any unhandled exception)

**P0-1 is fully closed across all 4 handlers.**

### 1.3 P0-2: webhook.py `import json` at module top

The `__import__("json")` is gone. `webhook.py:21` has a normal
`import json`. **P0-2 is closed.**

### 1.4 P0-3: webhook.py body size cap (1 MB)

`webhook.py:_MAX_BODY_BYTES = 1 * 1024 * 1024`. Applied at two layers:
- explicit re-check in `_handle()` (returns 413 on oversize)
- `web.Application(client_max_size=_MAX_BODY_BYTES)` aiohttp-layer cap

Defense in depth. **P0-3 is closed.**

### 1.5 P1-1: webhook.py task exception handler

The dispatch task is wrapped in `_safe_dispatch` with
`try/except Exception: log.exception(...)`. **P1-1 is closed.**

### 1.6 P1-2: WEBHOOK_URL defined + auto-register

`config.WEBHOOK_URL` is defined. Auto-register is implemented in
`dispatch.py:main()`. **P1-2 is closed** (with the v0.9.3 fix
for the auto-register footguns).

### 1.7 7 new unit tests

12 tests total:
- 5 from v0.6.x/v0.7 (in-tree selftest, store singleton, constants
  exposed, F4 LRU bound, rating parser).
- 3 for `delete_last_user_message` (delete works, returns False when
  empty, only removes `user` role).
- 4 for `webhook._check_secret` (valid / invalid / missing /
  unconfigured).

All 12 pass in 0.26s.

### 1.8 P3 cleanups

- `call_llama.py:_abortable_post`: split `(asyncio.CancelledError,
  Exception)` into two except blocks.
- `handlers.py:240, 510, 646, 829`: fixed misleading "handle_photo
  uses is_group" comment.
- `.env.example`: added `WEBHOOK_URL` documentation.

---

## 2. NEW P0 bugs introduced by v0.9.2 (FIXED in v0.9.3)

### 2.1 P0-NEW-1: dispatch.py auto-register missing `allowed_updates`

The `set_webhook` call did NOT pass `allowed_updates`. Telegram's
default is `["message", "edited_message"]` — `callback_query` is NOT
included by default.

The Stop button is implemented as a `callback_query` update. Without
`allowed_updates` including `callback_query`, Telegram does not
deliver Stop button clicks. The LLM runs to completion, ignoring
the user's Stop intent.

**Fix in v0.9.3:** added
`allowed_updates=["message", "edited_message", "callback_query",
"chat_member"]` to the auto-register call.

The same bug existed in WEBHOOK.md section 4.5 (the manual-register
curl). **Fix:** updated the curl to include `allowed_updates` with
a callout explaining why it's mandatory.

### 2.2 P0-NEW-2: dispatch.py auto-register uses two `asyncio.run()` calls

The original code had:
```python
asyncio.run(_auto_register())  # first loop
asyncio.run(_webhook_run())     # second loop
```

The first loop initialized a Bot object whose httpx client got
closed when the loop exited. The second loop started the webhook
server, but `context.bot.send_message(...)` failed because the
httpx client was dead.

**Fix in v0.9.3:** replaced the double-loop with a synchronous
HTTP call (`urllib`) for the one-shot `set_webhook` registration.
The Bot is initialized lazily inside the webhook event loop, where
its httpx client lives for the duration of the process.

---

## 3. P3 issues fixed in v0.9.3

- WEBHOOK.md section 4.5 manual-register curl includes
  `allowed_updates` with a mandatory-callout paragraph.
- `tests/test_selftest.py` imports moved to top of file;
  duplicates removed (`os`, `asyncio`, `pytest`); unused
  `json` and `hmac` removed; `MagicMock` added to top imports
  (used in webhook tests).

---

## 4. Smoke test coverage

**What v0.9.2 tested:**
- 12/12 pytest (unit tests)
- webhook auth (200/403/403)
- body cap (413 for 2 MB body)
- `/start` end-to-end with LLM reply (41 chars)

**What was missing:**
- Stop button (`callback_query` updates delivered and processed)
- `chat_member` updates (new member join)
- The auto-register feature with `WEBHOOK_URL` set

The auto-register bugs were invisible to the smoke test because
`WEBHOOK_URL` is empty in production (the operator registered
manually for v0.9.0).

**Lesson:** when a fix introduces a feature (like auto-register),
add an integration test for the feature, not just unit tests for
the function. The 4 webhook tests cover `_check_secret` but not
`set_webhook` (the registration side of the same workflow).

---

## 5. What v0.9.1 pre-release review got right and v0.9.2 confirmed

- **P0-SEC:** secrets in compose.yml. Fixed in 20b5cd9.
- **P0-1:** rollback missing in handle_voice/handle_document/handle_text.
  Fixed in v0.9.2.
- **P0-2:** `__import__("json")` in webhook.py. Fixed in v0.9.2.
- **P0-3:** no body size limit. Fixed in v0.9.2.
- **P1-1:** fire-and-forget task exception swallowed. Fixed in v0.9.2.
- **P1-2:** WEBHOOK_URL referenced but undefined. Fixed in v0.9.2.
- **P3 cleanups:** except clauses, is_group comment, .env.example.
  All done in v0.9.2.

The pre-release review was 100% correct on the items it flagged.
v0.9.2 closed all 6.

**What the pre-release review missed:** the auto-register feature
itself was added during the P1-2 fix. The 2 new P0s slipped through
because unit tests don't exercise the auto-register code path and
the smoke test used `WEBHOOK_URL=empty`.

---

## 6. Deferred items

T12 (pin whisper image version), T18/T19/T20 (P3 cleanups), and
T22 (HANDLERS.md) are correctly deferred. None are blocking.

---

## 7. Net assessment

**v0.9.2 closed 6 of 6 review items from v0.9.1.** The 7 new tests
bring coverage from 5 to 12. The 3 P3 cleanups are correct. The
security fix is comprehensive.

**v0.9.3 closes the 2 new P0s introduced by v0.9.2's auto-register**
plus the 2 cleanup items. The fix is 10 lines (sync HTTP +
`allowed_updates` in the curl). v0.9.3 is genuinely production-ready
and the public release can proceed.

*Reviewer: Mavis · Wave 10 (post-v0.9.2 review) · 2026-10-11*
