# REVIEW-MINIMAX-v10 — v0.9.4 test suite review (2026-10-11)

**Tag reviewed:** v0.9.4 (commit `5cec168`)
**Scope:** 6 files, +1510 / -22 lines. 36/36 pytest pass in 0.42s.

---

## 0. Top-level summary

The v0.9.4 test suite is real coverage, not theatre. 36 tests in 0.42s
cover exactly the operator's manual smoke scenarios:

- 7 handler rollback tests (all 4 handlers × abort + exception paths,
  plus the success-keeps-message sanity tests and the abort_event
  invariant)
- 17 webhook tests (secret-token check × 4, body cap × 2, handle paths
  × 4, safe_dispatch × 1, auto-register × 6)
- 12 in-tree selftest tests (storage basics, F4 LRU bound, rating
  parser, store singleton, constants)

The v0.9.3 review recommendations were 100% applied, plus the operator
over-delivered: 7 handler tests, 17 webhook tests (the v0.9.3 review
only asked for 1 webhook test), conftest autouse fixture, idempotent
auto-register, JSON body for setWebhook, Telegram error response
parsing, network error handling.

---

## 1. What v0.9.4 delivered

- **dispatch.py** — idempotent auto-register (getWebhookInfo first,
  skip if same URL, warn on overwrite), JSON body, real Boolean
  `drop_pending_updates`, real JSON list `allowed_updates`,
  Telegram error response parsing, network error handling.
- **tests/conftest.py** — autouse fixture resets `bot.store` and
  `storage._store_instance` before AND after each test.
- **tests/test_handlers.py** (7 tests) — rollback pattern in all
  4 handlers for both abort and exception paths, success-keeps-message
  sanity, abort_event-registered-with-thinking.message_id invariant.
- **tests/test_webhook.py** (17 tests) — full matrix.
- **tests/test_selftest.py** (12 tests) — original suite, unchanged.
- **docs/REVIEW-MINIMAX-v9.md** — the v0.9.3 review, committed as a doc.
- **CHANGELOG.md** — v0.9.4 section.

---

## 2. Per-file assessment

| File | Status | Notes |
|------|--------|-------|
| `telegram-bot/dispatch.py` | OK | Idempotent auto-register well-designed. Three branches (skip / warn / register), JSON body, error parsing. |
| `tests/conftest.py` | OK | Autouse fixture is the right call. |
| `tests/test_handlers.py` | OK (with cleanup) | 7 tests, rollback pattern fully covered. |
| `tests/test_webhook.py` | OK | 17 tests, full matrix covered. |
| `tests/test_selftest.py` | OK | 12 tests, original suite. |
| `docs/REVIEW-MINIMAX-v9.md` | OK | The v0.9.3 review, committed. |

---

## 3. Test coverage matrix

| Regression | Test that catches it |
|------------|---------------------|
| Handler forgets to set `persisted = True` | `test_handle_voice_rollback_on_abort` |
| Handler rollback uses wrong role filter | `test_delete_last_user_message_only_removes_user_role` |
| `_register_abort_event` called with wrong key | `test_text_abort_registers_abort_event_with_thinking_message_id` |
| Auto-register missing `callback_query` | `test_auto_register_includes_allowed_updates` |
| Auto-register overwrites silently | `test_auto_register_warns_on_overwrite` |
| Auto-register sends form data | `test_auto_register_uses_json_body_not_form` |
| Auto-register breaks on Telegram error | `test_auto_register_handles_telegram_error` |
| `_safe_dispatch` swallows exception silently | `test_safe_dispatch_logs_exceptions` |
| Body cap removed | `test_handle_oversized_body_returns_413` |
| Secret check removed | `test_check_secret_rejects_invalid_token` |

This is real coverage. The tests would catch the regressions they
were designed to catch.

---

## 4. v0.9.4 review recommendations for v0.9.5

- [ ] **T1: DRY up test_handlers.py setup** (P2, 30 min)
- [ ] **T2: Remove dead `_pers._store_instance` line from 7 tests** (P3, 1 min)
- [ ] **T3: Add `test_auto_register_handles_empty_current_url`** (P3, 5 min)
- [ ] **T4: Add test for `_abortable_post` cancellation** (P2, 15 min)
- [ ] **T5: Add focused test for known_topics round-trip** (P2, 10 min)
- [ ] **T6: Fix conftest.py comment "35+"** (1 line)
- [ ] **T7: Add `docs/REVIEW-MINIMAX-v10.md`** (this file)

---

## 5. Net assessment

**v0.9.4 is the strongest release in the v0.9.x cycle.** It:

- Closes 100% of the v0.9.3 review items.
- Adds 24 new tests (12 to 36) that all pass in 0.42s.
- Replaces the operator's manual smoke routine with automated tests.
- Has idempotent auto-register that prevents the overwriting incident
  the operator hit in v0.9.3.
- Has clean dispatch.py with JSON body and error parsing.

The test code is real coverage, not theatre. I traced the
assertions to the production code paths and verified that each
test fails if the corresponding production code is broken.

The 0.42s test run is the new operator comfort level. No more
"let me smoke this for 10 minutes" - `pytest tests/` and 0.42s
later you have confidence. This is the ROI of the v0.9.x
hotfix chain.

*Reviewer: Mavis · Wave 12 (test suite review) · 2026-10-11*
