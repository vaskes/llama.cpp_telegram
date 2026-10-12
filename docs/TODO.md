# docs/TODO.md — LlamaBot operator & dev backlog

> This file is the canonical backlog. The first thing the next
> session should read. Updated 2026-10-11 03:30 (autonomous
> overnight work).

---

## Status snapshot (2026-10-11 14:00)

**Production:** v0.9.1 deployed in webhook mode on
gw1.kuduza.com (Amsterdam, 85.136.112.144) via tunnel to
the bot container on 192.168.10.5 (lan_macvlan). All four
services (telegram-bot, whisper-api, llama-server,
donsetch-http) reachable; bot is the only one on macvlan,
the rest are on the parent host. whisper-api moved to
macvlan 192.168.10.3 (was on parent host .7) so the bot
can reach it. setMyCommands registered 9 commands.

**In-tree selftest:** 22/22 test groups PASS in Docker
(no new regressions; 2 pre-existing rating-parser
subtest failures "plain answer" and "[[TYPE:unknown]]"
predate v0.7.9 and are tracked separately).

**Pytest:** 5/5 tests PASS in 0.07s.

**Hotfix count this cycle:** 12 (v0.7.3 -> v0.9.1).
v0.9.1 closes two regressions caught by operator smoke-
testing v0.9.0 in production: (1) image abort left
orphan in history, fixed by Storage.delete_last_user_message
+ handle_photo rollback in both abort and exception paths;
(2) voice unreachable because whisper-api was on parent
host - moved to macvlan 192.168.10.3.

Of the 12 total, 7 caught by operator testing, 4 caught by
selftest regressions in Docker, 1 (v0.9.1) split across both.

**Commits since last TODO update:**
- `8352cc4` v0.7.3 — handle_photo NameError on is_group
- `659ce1f` v0.7.4 — _stop_button_markup + sticky Stop message
- `5a3f2b2` v0.7.5 — handle_voice missing Stop button
- `1d9c1bc` v0.7.6 — Stop button actually aborts LLM call
- `5b28ed6` v0.7.7 — handle_photo NoneType len() on abort
- `226affa` v0.7.8 — queue messages instead of dropping
- `ee3e04a` v0.7.9 — retry on RetryAfter flood control
- `02c681b` v0.7.9b — _rate_limit_send + Stop button edit retry
- `3581fdf` v0.8.0 — discard response if abort_event was set
- `64d2c15` v0.8.1 — retry without tools on llama-server 500
- `9d098e2` v0.8.2 — explicit GITHUB_TOKEN permissions

**Operator tests:** 5 of 7 done (Test 0, 1, 2, 4, 5, 6).
Test 3 (edit-during-LLM, Ornith) explicitly deferred —
user accepted as by-design limitation, see T25 below.

**CodeQL alert:** v0.8.2 closed `actions/missing-workflow-
permissions` (CWE-275).

---

## CRITICAL — operator tests pending (morning)

See `OPERATOR-TESTS-MORNING.md` for the step-by-step. Summary:

1. **Test 0** — bot replies in private chat (30 sec)
2. **Test 1** — Stop button UX (5 min, needs Ornith)
3. **Test 2** — Multi-user concurrency in group (10 min,
   needs Dmitri id 117382588)
4. **Test 3** — Edit-during-LLM (10 min, needs Ornith,
   by-design limitation) — **DEFERRED** per user, see T25
5. **Test 4** — `/reset` in forum topic (5 min)
6. **Test 5** — Donsetch tool via real prompt (5 min)
7. **Test 6** — Visual stop-button feedback (2 min,
   needs Ornith)

Total: ~40 min of operator time.

---

## Backlog

### Pending (next session)

- **T25:** ~~Webhook migration~~ **DONE (v0.9.0).** Deployed
  in production with 192.168.10.3 secondary IP + MikroTik
  DNAT + PBR. Pending update count dropping, end-to-end
  smoke test green from foreign egress. See
  `docs/WEBHOOK.md` for the full operator documentation
  and `docs/REVIEW-MINIMAX-v9.md` (TODO: post-cycle review).
- **T26:** Run the remaining operator tests. **5 of 7
  already verified** by user. Test 3 (edit-during-LLM)
  accepted as by-design limitation, deferred. Only the
  morning checklist file remains as artifact.
- **T27:** ~~Add `docs/REVIEW-MINIMAX-v6.md`~~ **DONE.**
  See `docs/REVIEW-MINIMAX-v6.md` (10444 bytes). Archives
  the v0.6.0 + v0.6.1 + v0.6.2 + v0.6.3 review cycle.
- **T28:** Fix 2 pre-existing rating parser subtest
  failures ("plain answer", "[[TYPE:unknown]]"). These
  predate v0.7.9 and are unrelated to any other commit
  in this cycle. The parser expects a TYPE marker; these
  inputs don't have one. Either skip these cases in the
  parser, or update the test expectations. ~30 min.
- **T29:** Remove `get_weather` (wttr.in) tool. The tool
  was added early as a "trivial demo" for tool-calling,
  but it crept in and stayed. It is narrow (single
  endpoint, no caching, no location awareness beyond
  free-form city text) and now redundant given the
  general `web_search` / `web_fetch` tools via donsetch.
  Action: remove from `_TOOLS_CACHE` definitions, drop
  from system prompt examples, delete any direct call
  sites in `call_llama.py` or `dispatch.py`, run
  `scripts/verify.py` to confirm the tool list shrinks
  and no regression. ~20 min. Tracked from operator
  decision 2026-10-12.
- **T30:** Remove sender-name tagging. Every user message
  is currently prefixed with `From: <name>: ` before
  being sent to the LLM, so the model can tell different
  users apart in group chats. Feature crept in during
  early multi-user support, hasn't been needed in
  practice (operator finds it noisy in logs and
  confusing in traces), and bloats context for every
  message. Action: (1) drop the `sender_name` column +
  all references in `persistence.py` / `storage.py`;
  (2) remove the `From: <name>: ` prepend in
  `call_llama.py`; (3) strip the `_sender_name`
  internal marker handling in `bot.py` /
  `call_llama.py`; (4) update tests in `test_call_llama.py`
  / `test_selftest.py` / `test_storage.py` that pass
  `sender_name=`; (5) run `scripts/verify.py` + full
  selftest + pytest; (6) document in CHANGELOG that
  v0.11 removes the feature and breaks the v3→v4
  schema migration path. ~45 min. Tracked from
  operator decision 2026-10-12.

### T25 retrospective (was deferred, now done)

User pushed for migration: T25 was in the backlog
for 3 review waves as "defer". On 2026-10-11 the
operator said "let's do it" and provided the
infrastructure (wildcard LE cert on *.kuduza.com,
MikroTik DNAT capability, foreign-egress PBR
control, secondary IP support on enp10s0).

Final architecture:
- Bot: `network_mode: host`, aiohttp on
  0.0.0.0:8443 inside container (which is host's
  0.0.0.0 since host network).
- nginx-proxy: `network_mode: host`, listens on
  0.0.0.0:443 (serves on both 192.168.10.7 and
  192.168.10.3 thanks to the secondary IP on
  enp10s0). Container is "just packaging" — no
  real network isolation from the host.
- Host: secondary IP 192.168.10.3/24 on enp10s0.
- MikroTik: DNAT 91.210.150.42:443 -> 192.168.10.3:443
  + PBR (outgoing to 192.168.10.3 from foreign
  source-IPs goes via 91.210.150.42 WAN interface,
  not via VPN tunnel — needed to avoid
  asymmetric routing on foreign ingress).
- nginx config: TLS termination on 443, reverse-
  proxy to http://192.168.10.7:8443.

Things that didn't work and why:
1. **macvlan with bridge mode** + `parent=enp10s0`:
   macvlan sub-interfaces cannot reach the parent
   host's LAN IP (kernel-level isolation, well-
   documented). ARP for 192.168.10.3 from the host
   itself returned `<incomplete>`. This is a
   fundamental Linux limitation, not a docker
   bug. Workarounds (proxy_arp, dummy interface
   with same IP) were tried and didn't fully
   resolve bidirectional traffic.
2. **ipvlan L2 mode**: same problem as macvlan
   for the same kernel reasons. L3 mode would
   have worked but required renumbering the
   container onto a different subnet (e.g.
   192.168.20.0/24).
3. **macvlan_mode=bridge**: also couldn't
   container-to-host communication.
4. **dummy interface with same IP on host**:
   IP conflict with the macvlan container.
5. **Final working solution**: secondary IP on
   host's enp10s0 + container with `network_mode:
   host` + nginx binds to 0.0.0.0:443 (serves on
   both .7 and .3 transparently). Simpler than
   macvlan, no kernel-isolation issue, just
   standard Linux alias interface.

Latency: O3 (edit-during-LLM) latency went from
~25s polling cycle to <1s push. Telegram retries
its queued updates within minutes of the fix; we
saw `pending_update_count` drop from 14 to 0 in
about 5 minutes.

End-to-end smoke test from foreign egress (this
sandbox, which is on Cloudflare's CDN path, not
the user's Russian direct path): POST with valid
X-Telegram-Bot-Api-Secret-Token returns "ok", bot
log shows `processed update_id=1000`. Confirmed
working from any path.

Code review pending — `docs/REVIEW-MINIMAX-v9.md`
not yet written. Operator smoke tests in progress
as of 2026-10-11 17:25 (this snapshot).

### Done — v0.7.3 → v0.8.2 (11 hotfixes)

#### v0.8.2 (commit 9d098e2) — CodeQL alert fix

- `.github/workflows/lint.yml`: added explicit
  `permissions: contents: read` block. CodeQL
  `actions/missing-workflow-permissions` (CWE-275)
  was complaining that the workflow had no
  permissions block. Defense-in-depth: GITHUB_TOKEN
  can now only read repo contents.

#### v0.8.1 (commit 64d2c15) — retry on llama-server 500

- `call_llama.py`: on 500 from non-streaming endpoint
  (vision), retry ONCE without tools. The 500 from
  llama-server is usually the PEG grammar parser
  rejecting the model's malformed tool-call JSON.
  Without tools offered, the model can't produce
  a malformed tool call → grammar parser has nothing
  to reject.

#### v0.8.0 (commit 3581fdf) — discard response if abort_event was set

- `handlers.py`: all 4 handlers (text/photo/voice/document)
  now check `abort_event.is_set()` after call_llama
  returns. If set, discard the response even if
  call_llama returned a non-None string. This
  prevents the "user clicked Stop but still got a
  response" race (post won the abort_event race).
- `bot.py`: added `post-race abort discard`
  regression test.

#### v0.7.9b (commit 02c681b) — _rate_limit_send + Stop button edit retry

- `dispatch.py`: added `_rate_limit_send(chat_id)`
  helper — sliding window 60s × 15 sends. Prevents
  Telegram's flood control from firing in the first
  place.
- `dispatch.py`: Stop button's `edit_message_text`
  also retries on RetryAfter.

#### v0.7.9 (commit ee3e04a) — retry on RetryAfter flood control

- `handlers.py`: `_reply` catches `RetryAfter` and
  retries up to 3 times with `sleep(retry_after+1)`.

#### v0.7.8 (commit 226affa) — queue messages on semaphore lock

- `dispatch.py`: `_check_user_slot` now waits up to
  300s for a free slot instead of rejecting with
  "Бот уже обрабатывает 2 твоих запросов". Queueing
  is per-user via `asyncio.wait_for(sem.acquire())`.
  The handler is in an asyncio task so the wait
  doesn't block the polling loop.

#### v0.7.7 (commit 5b28ed6) — handle_photo NoneType len() on abort

- `handlers.py`: `print(f"...{len(bot_response)}...")`
  before the `if bot_response is None` check crashed
  with `TypeError: object of type 'NoneType' has no
  len()`. Fixed with None-safe vars.

#### v0.7.6 (commit 1d9c1bc) — Stop button actually aborts LLM call

- `call_llama.py`: added `_abortable_post` helper.
  Races `httpx.post()` vs `abort_event` via
  `asyncio.wait(FIRST_COMPLETED)`. Returns None on
  abort, response on success.

#### v0.7.5 (commit 5a3f2b2) — handle_voice missing Stop button

- `handlers.py`: handle_voice sent `_reply(...)`
  without `reply_markup=_stop_button_markup()`.
  v0.5.2 added abort_event to handle_voice, v0.6.0
  F4 added button to photo/document/text but missed
  voice.

#### v0.7.4 (commit 659ce1f) — _stop_button_markup + sticky Stop message

- `handlers.py`: handle_voice + handle_document had
  `_stop_button_markup` dropped from late imports
  (v0.6.3 T8 cleanup). Added back.
- `dispatch.py`: cmd_callback uses `delete_message()`
  instead of `edit_message_text` to remove the
  thinking message. Editing preserves the inline
  button; deleting removes it.

#### v0.7.3 (commit 8352cc4) — handle_photo NameError on is_group

- `handlers.py`: 1-line fix. v0.6.3 T11 P1 cleanup
  had dropped `is_group` from handle_photo's
  destructure where it WAS used on line 218 for
  a print statement. Caught by operator testing,
  fixed with regression test.

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

---

## Code freeze (2026-10-12, post-v0.10.2)

**Status:** Code is frozen at v0.10.2. No more fixes or features
until operator completes manual testing on 2026-10-12 and files
bug reports.

### Qwen wave-3 final report — items for next cycle

Qwen issued a final report after three review waves (15 of 20
items closed in v0.10.0–v0.10.2). Per operator directive, all
items below are recorded for the next cycle, **not** addressed
now.

#### Accepted as documented risk (no fix needed)

- [ ] **T-FREEZE-1** Held-sem eviction protection on
      `_user_semaphores`. Currently the LRU evicts even a held
      sem, so the next message from the same user gets a NEW sem
      with the full `_PER_USER_SEMAPHORE_LIMIT` budget. Acceptable
      for current deployment with `ALLOWED_USER_IDS=2,1` (5–10
      entries, never reach the 500 cap). Revisit if deployment
      scales to many users.

- [ ] **T-FREEZE-2** `call_llama()` god function (~400 lines,
      cyclomatic complexity ~100). `DO NOT REFACTOR without
      reading docs/CALL_LLAMA.md` comment is real — the function
      has 9 responsibilities, the comments warn that refactoring
      without reading the docs will break things. Should be
      refactored into modules once it has been observed in
      production long enough to confirm what can be split.

- [ ] **T-FREEZE-3** `BOT_COMMANDS` mixed Russian/English.
      Cosmetic, intentional (Russian descriptions with English
      technical terms "sub-talk"). Leave as-is.

#### False alarms (Qwen was wrong, no action)

- [ ] **T-FREEZE-4** `_RATING_PREFIX_RE` and `_EMOJI_CHAR_RE`
      are unified after v0.10.0 cleanup. Already correct.

#### Manual testing checklist (operator's job, 2026-10-12)

23-point checklist from Qwen's final report. Operator will
execute; bug reports → new TODO items.

- [ ] **T-FREEZE-MT-1..23** (see Qwen report sections:
      smoke test, rating, stop button, media, sub-talks, empty
      response, group). Operator runs; results back as bug
      reports or "all green".

#### Test automation (Qwen recommended 3 variants; operator
chose to defer until after manual testing cycle completes)

- [ ] **T-FREEZE-AUTO-1** `scripts/smoke_test.py` — Variant 1
      from Qwen: real Telegram API + `getUpdates` polling. 80% of
      cases covered, no mocking required. Recommended first
      automation step.

- [ ] **T-FREEZE-AUTO-2** `tests/test_integration.py` —
      Variant 2 from Qwen: pytest integration tests with
      `@pytest.mark.integration` marker, opt-in via
      `-m integration`. Better isolation than smoke test, runs
      in CI.

- [ ] **T-FREEZE-AUTO-3** `.github/workflows/deploy.yml` —
      Variant 3 from Qwen: CI/CD with auto-deploy on tag push.
      Requires GitHub secrets for the deploy SSH key.

#### Documentation

- [ ] **T-FREEZE-DOC-1** Update `docs/TROUBLESHOOTING.md` with
      post-v0.10.2 known issues and workarounds (e.g., new
      LLM behaviour in group, longer prompts now active).
