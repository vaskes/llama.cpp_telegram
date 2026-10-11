# docs/TODO.md — LlamaBot operator & dev backlog

> This file is the canonical backlog. The first thing the next
> session should read. Updated 2026-10-11 03:30 (autonomous
> overnight work).

---

## Status snapshot (2026-10-11 03:30)

**Production:** v0.7.2 deployed and stable. Container
`telegram-bot` running. setMyCommands registered 9 commands.

**In-tree selftest:** 20/20 test groups PASS (including the
real donsetch integration test that was previously SKIPPED).

**Pytest:** 5/5 tests PASS in 0.07s. CI entry point ready
(`pip install -r requirements-dev.txt && pytest`).

**Commits this session:**
- `58b8be3` v0.6.3 — T1d LRU test + T7 raw-insert fix +
  T8 rest→body + 6 P1s
- `7664f93` v0.7 — T12-T18 P2 cleanups + fix v0.6.3
  late-import regression
- `850bc5e` v0.7.1 — fix 2 pre-existing selftest bugs (T19)
- `aaed870` v0.7.2 — pytest integration (T20) + donsetch
  re-export (T24)

**Operator tests:** 6 still pending — see
`OPERATOR-TESTS-MORNING.md` for the full list.

---

## CRITICAL — operator tests pending (morning)

See `OPERATOR-TESTS-MORNING.md` for the step-by-step. Summary:

1. **Test 0** — bot replies in private chat (30 sec)
2. **Test 1** — Stop button UX (5 min, needs Ornith)
3. **Test 2** — Multi-user concurrency in group (10 min,
   needs Dmitri id 117382588)
4. **Test 3** — Edit-during-LLM (10 min, needs Ornith,
   by-design limitation)
5. **Test 4** — `/reset` in forum topic (5 min)
6. **Test 5** — Donsetch tool via real prompt (5 min)
7. **Test 6** — Visual stop-button feedback (2 min,
   needs Ornith)

Total: ~40 min of operator time.

---

## Backlog

### v0.7.3 / v0.7.4 (next)

- **T25:** Webhook migration. Currently using long-polling.
  O3 (edit-during-LLM) can be properly fixed only with
  webhooks. P1-ish.
- **T26:** Run the operator tests O1-O6 (above) and
  verify behavior preserved. (P3 user-driven.)
- **T27:** Add `docs/REVIEW-MINIMAX-v6.md` archiving the
  v0.6.0 + v0.6.1 + v0.6.2 + v0.6.3 review cycle.

### Done — v0.5.0 → v0.7.2

#### v0.7.2 (commit aaed870) — T20 + T24

- T20: pytest integration. `tests/` directory with
  `conftest.py` (sys.path setup) and `test_selftest.py`
  (5 tests wrapping the in-tree selftest + the key
  regression tests). `pytest.ini` configures asyncio.
  `requirements-dev.txt` lists `pytest` and
  `pytest-asyncio`. 5/5 PASS in 0.07s.
- T24: `donsetch_call` and `_donsetch_init` re-exported
  from `bot`. The `asyncio.run(_rt())` call in the
  donsetch test changed to `await _rt()` (in-tree
  selftest already runs in an event loop). The real
  donsetch integration test now actually runs against
  the live MCP server:
    - `web_search: 620 chars, "Search results" present`
    - `web_fetch: 195 chars, markdown content delivered`

#### v0.7.1 (commit 850bc5e) — T19

- T19.1: sender-name flow test — changed `[]` to `.get()`
  for `_sender_name` access. Defensive against future
  schema changes.
- T19.2: `_download_with_limit` test — rewrote to use
  `_FakeUpdate` with `effective_chat = None` and
  `_FakeMessage.reply_text` capture. The F3 module split
  broke the previous `_b._reply = _fake_reply` patch
  pattern (patching `bot._reply` no longer affects
  `handlers._reply` local name).
- Verified: 20/20 test groups pass.

#### v0.7 (commit 7664f93) — T12-T18 + regression fix

- T12: RATING_EMOJI and BLOAT_EMOJI deleted from bot.py
  (prompts.py is canonical).
- T13: dispatch.py "Stage 6" stale comment replaced.
- T16: persistence.py import order cleaned (no blank
  line between two project imports).
- T17: persistence.py docstring says `storage.get_store()`
  (was `storage.store`).
- T14, T15, T18: already done in earlier releases
  (rating.py shims, state.py imports, storage column
  name `rating` singular).
- **Regression fix:** v0.6.3 hotfix's per-function
  minimal late imports in `handlers.py` accidentally
  dropped `reject_if_unauthorized` from `_route_to_thread`.
  Restored. The PTB "No error handlers are registered"
  log in the v0.6.3 selftest was the regression signal;
  the in-tree selftest masked it because the test path
  didn't go through `_route_to_thread` directly.

#### v0.6.3 (commit 58b8be3) — T1d + T7 + T8 + 6 P1s

- T1d: F4 LRU bound regression test added to in-tree
  selftest (250 inserts → 200 entries).
- T7: existing abort_event test changed from raw insert
  to `_register_abort_event` (was the v0.6.0 P0-1
  pattern).
- T8: rating parser test `rest` → `body` (KeyError
  since v0.6.0).
- T6: call_llama.py duplicate `_discover_default_model`
  removed.
- T7: MODEL discovery at import → lazy `_ensure_model()`.
- T8: 8× copy-pasted late imports in handlers.py trimmed
  to per-function minimal.
- T9: prompts.py duplicate `import re` consolidated.
- T10: handlers.py 5 unused call_llama imports trimmed.
- T11: handlers.py `is_group` dropped in 3/4 handlers.
- CHANGELOG.md: v0.6.1 + v0.6.2 sections added.
- docs/TODO.md: F4 marked DONE.

#### v0.6.2 (commit 489841a) — T1a + T1b + T2-T6

- T1a: Re-export `bot.store` via `get_store()`.
- T1b: Re-export `_PER_USER_SEMAPHORE_LIMIT` and
  `_GLOBAL_LLM_SEM_LIMIT` to bot.py.
- T2-T6: dead code cleanup (OrderedDict, storage import,
  DB_PATH, _apply_reaction, 3× duplicate caps, _s()
  wrapper).
- **Known gap:** T1d (LRU test) was claimed in commit
  message but not actually added. Fixed in v0.6.3.

#### v0.6.1 (commit ea3d484) — 5 P0s

- F4 LRU bound: handlers use `_register_abort_event`.
- `_abort_events` moved to state.py.
- bot.py no longer shadows config.py constants.
- Single Storage instance via `get_store()`.
- Dockerfile `COPY *.py ./`.

#### v0.6.0 (commit 4e24b2d) — F3 module split

- bot.py → 8 modules (config, prompts, persistence,
  rating, call_llama, handlers, dispatch, state).

#### v0.5.2 (commit fbd9f42)

- F1 stale comment fix.
- F2 semaphore selftest added.

#### v0.5.0

- Qwen migration, GROUP_CONTEXT, react_to_message.

---

## Pattern observation (3 hotfixes ago)

3 hotfixes in a row (v0.6.1, v0.6.2, v0.6.3) had
documentation debt and verification drift. v0.6.3 + v0.7 +
v0.7.1 + v0.7.2 collectively:

- Add 2 missing CHANGELOG sections (v0.6.1 + v0.6.2)
- Mark F4 DONE in docs/TODO.md
- Add the actual LRU test (claimed in v0.6.2, delivered
  in v0.6.3)
- Add the actual fix for the abort_event test
  (raw-insert → _register_abort_event)
- Fix the rating parser test
- Add pytest as the new safety net
- Add donsetch_call re-export so the integration test
  actually runs

After v0.7.2 the pattern should stop: every claim in the
commit message is verified by a passing test, every test
in the in-tree selftest runs, and pytest is the CI entry
point.
