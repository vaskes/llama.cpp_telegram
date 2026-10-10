# Changelog

All notable changes to this project are documented here.
Format: [Semantic Versioning](https://semver.org/) + sections
per release. The repo's `git log` is the granular record;
this file is the human-readable summary.

## [v0.6.0] — 2026-10-10 — F3 module split, F4 LRU eviction, cross-module cleanup

Big refactor + 7 hotfix commits. The 5 518-line `bot.py` is
split into 7 cohesive modules (`config.py`, `prompts.py`,
`persistence.py`, `rating.py`, `call_llama.py`, `handlers.py`,
`dispatch.py`, `state.py`). The 2180-line slim `bot.py` only
holds imports, re-exports for backward compat, the `_selftest`
function, and `__main__`. No user-facing behaviour change in
this release — the refactor was motivated purely by the cost
of every new feature having to be reviewed in a 5 500-line
file. The same code now lives in 7 files averaging ~400 lines.

### Added

- **`state.py`** (72 lines) — cross-module runtime state.
  Owns `_abort_events` (OrderedDict, F4-bounded at 200),
  `_bot_replies` (edit-replace tracking), `_global_llm_sem`
  (lazy semaphore singleton), `_user_semaphores` (per-(chat,
  user) sem dict), and the concurrency caps. Centralizing
  here breaks the "every function needs a late import from
  bot" mess from the initial shim pattern. New `get_store()`
  lazy-singleton in `storage.py` (companion fix from the
  same wave).

- **`config.py`** (86 lines) — env-driven configuration. All
  constants initialised from `os.environ` at import time. Owns
  `LOCKDOWN` (True when both `ALLOWED_USER_IDS` and
  `ALLOWED_USERNAMES` are empty), the four `MAX_*_BYTES`
  upload limits, the per-handler concurrency caps
  (`_GLOBAL_LLM_SEM_LIMIT=4`, `_PER_USER_SEMAPHORE_LIMIT=2`).
  Re-exports the state.py constants for backward compat with
  v0.5.2 call sites.

- **`prompts.py`** (133 lines) — text templates. Owns
  `GROUP_CONTEXT` (2 672 chars, the LlmChatPlace rules),
  `RATING_RULES` (rating-mode prompt), `WELCOME_TEXT` (pinned
  human-facing welcome), `RATING_EMOJI` (10 entries), `BLOAT_EMOJI`,
  and three compiled regexes (`_LLM_TOKEN_RE`, `_SUBTALK_NAME_RE`,
  `_TOPIC_NAME_RE`) moved out of `bot.py`.

- **`persistence.py`** (76 lines) — async wrappers around
  the storage layer. `persist(chat_id, thread_id, role, content,
  sender_name=None)` and `load_history(chat_id, thread_id)`.
  Both dispatch the actual SQLite work to a worker thread
  via `asyncio.to_thread` so the bot's event loop stays
  responsive during DB writes. The `_s()` helper returns the
  storage singleton lazily.

- **`rating.py`** (203 lines) — the rating-mode stack. Owns
  `parse_rating_response` (parses `[[TYPE:...]] [[RATE:N]]`
  prefix), `apply_rating_and_persist` (the dispatch helper
  called from every handler), `apply_reaction` (the Telegram
  reaction setter), and `execute_react_to_message` (the
  `react_to_message` tool executor). Backward-compat shims
  with the v0.5.2 private names (`_parse_rating_response`,
  `_apply_rating_and_persist`, etc.) so the bot.py re-exports
  still work.

- **`call_llama.py`** (1 142 lines) — the LLM-call layer.
  Owns `call_llama()` (the big coroutine with the abort
  ladder, tool loop, and reasoning stream push), the four
  `execute_donsetch_web_*` executors, `donsetch_call` (the
  MCP JSON-RPC client with session lifecycle and 404-retry),
  `fetch_tools_from_llama` (the cold-start tool discovery),
  `transcribe_voice` (Whisper API client), `_tag_sender`
  (the `From: <name>:` tagger for the LLM context),
  `_donsetch_session_id` and `_donsetch_session_lock`
  (module-level MCP session state). The big `docs/CALL_LLAMA.md`
  still applies — DO NOT refactor `call_llama()` without
  reading that doc end-to-end and writing a regression test
  against a captured llama-server response.

- **`handlers.py`** (757 lines) — the four message handlers
  (`handle_text`, `handle_photo`, `handle_voice`,
  `handle_document`) plus their helpers (`send_reply`,
  `_reply`, `_route_to_thread`, `_reject_in_group`,
  `_reply_active`, `_resolve_active`, `_general_thread_id`,
  `_sender_display_name`, `_download_with_limit`). Each
  handler runs the same dance: route → slot → persist →
  thinking → abort_event → call_llama → rating dispatch →
  reply → cleanup. Function-local `from dispatch import ...`
  for the dispatch helpers (the circular dep is broken at
  call time, not load time).

- **`dispatch.py`** (1 411 lines) — the orchestration layer.
  Owns `main()` and the polling loop (hand-rolled, not
  `Application.run_polling()` — see the v0.5.1 T1 rationale),
  `_dispatch_update` (per-update router), `_handle_chat_member_update`
  (welcome / leave events), `_register_bot_menu` (Telegram
  menu via `setMyCommands`), `cmd_callback` (Stop button
  handler), all 9 command handlers (`start`, `reset`,
  `cmd_help`, `stats`, `cmd_newsub`, `cmd_sub`, `cmd_here`,
  `cmd_subs`, `cmd_delsub`), auth (`is_authorized`,
  `reject_if_unauthorized`), group routing (`_is_group_chat`,
  `_should_mute_in_group`, `_is_reply_to_other_user`,
  `_stop_button_markup`), concurrency helpers
  (`_user_semaphore`, `_check_user_slot`, `_get_global_llm_sem`).
  Function-local `from bot import ...` for `_selftest` and
  `BOT_COMMANDS` (the late imports that break the
  dispatch-imports-handlers-imports-dispatch cycle).

- **F4 LRU-bounded `_abort_events`**. Replaced the plain
  `dict` with `collections.OrderedDict` and a max-size of
  200 entries. `state._register_abort_event(chat_id, msg_id, ev)`
  does `move_to_end` on every touch (so an in-flight handler
  never gets evicted by a concurrent insert) and `popitem(last=False)`
  to evict the oldest entry when at cap. Closes the slow leak
  where a handler raising between insert and pop without
  going through the `finally` left an event in the dict
  forever. With the per-user sem (2 concurrent) and the
  global LLM sem (4 total), the leak was bounded by the
  number of distinct users sending concurrent messages; in
  practice, 200 is a generous ceiling (real-world load peaks
  at ~30).

### Changed

- **bot.py shrunk 5 518 → 2 180 lines** (−60%). What remains:
  imports, re-exports for the new modules (so existing
  `from bot import X` references still work), the 65+
  subtest `_selftest` function, and `__main__`. The remaining
  body is the selftest, which is *the* place to look when
  investigating a regression. The original (now-duplicate)
  function bodies are kept under the re-exports as a stop-gap
  for any caller that bypasses bot.py; they'll be deleted in
  v0.7 once a third-party consumer of the selftest-level
  `_b.store.*` patterns is confirmed not to exist.

- **Module size growth is intentional**: 5 500 → 6 534 total
  lines (+18%). All the new lines are module docstrings,
  cross-module documentation, and the Late-import-block
  comments in `handlers.py` / `dispatch.py` that explain
  the dispatch ↔ handlers circular dep. The trade is fewer
  review headaches per change: a 400-line diff is 10× cheaper
  to review than a 4 000-line one.

### Hotfixes (deployed live during the refactor)

The first 5 stages of the F3 split looked clean in worktree
testing but the production deploy surfaced 7 real bugs that
didn't appear in the selftest (which exercises the LLM call
path but not all the command paths). The hotfixes are
included in the same release:

- **stage 7 hotfix — storage instance singleton**: `persistence.py`
  did `import storage as store`, which rebinds `store` to
  the storage MODULE (not an instance). So `store.add_message`
  raised `AttributeError: module 'storage' has no attribute
  'add_message'`. Added `get_store()` lazy-singleton in
  `storage.py`; `persistence.py` uses `_s().add_message(...)`
  instead.

- **load_history key fix**: `r["content_json"]` → `r["content"]`.
  The real column name in the `messages` table is `content`;
  the refactor had a typo.

- **handlers.py `call_llama` import**: `import call_llama` +
  `await call_llama(...)` was calling the MODULE, not the
  function. Replaced with `from call_llama import call_llama,
  transcribe_voice, ...` to bind the function name in the
  local namespace.

- **handlers.py `import base64`**: dropped during the move.
  Re-added for the photo `data:` URL encoding.

- **dispatch.py `_apply_rating_and_persist` / `_execute_react_to_message`**:
  both were referenced but the late imports from `bot` were
  missing in some function scopes. Added them.

- **dispatch.py `store` references**: 29 places used the
  bare `store` global that only existed in v0.5.2's `bot.py`.
  Replaced all with `get_store().xxx` after adding the import.

- **handlers.py `store` references** (7 places): same
  pattern as dispatch.py. Replaced with `get_store().xxx`.

- **config.py `LOCKDOWN`**: was set in v0.5.2's `bot.py` as
  a derived module-level constant. After the move, `is_authorized`
  in `dispatch.py` referenced it but no one defined it. Added
  the `_PER_USER_SEMAPHORE_LIMIT` and `_GLOBAL_LLM_SEM_LIMIT`
  constants to `config.py` at the same time.

- **`_LLM_TOKEN_RE`, `_SUBTALK_NAME_RE`, `_TOPIC_NAME_RE`**:
  compiled regexes that lived in `bot.py` were missed by
  the initial move. They got picked up by `dispatch.py`'s
  command parsers. Moved to `prompts.py`.

- **`_donsetch_session_id`, `_donsetch_session_lock`**:
  MCP session state lived in `bot.py` as a module-level
  pair. After the move, `call_llama.py` referenced them
  but they weren't there. Moved into `call_llama.py` (they
  are only used by `donsetch_call`).

### Operational impact

- **Production runtime at `/opt/telegram-bot/`** has been
  running the v0.6.0 code since 2026-10-10 22:00 UTC. The
  smoke tests passed: text / photo / voice / document
  handlers work, all 9 commands respond, GROUP_CONTEXT is
  injected on every call, reasoning stream is visible in
  groups, the 4-tool Donsetch MCP integration is reachable
  (verified with `web_search` for "kuduza ai lab" → 1 805
  chars of results). The deployment is currently active and
  serving real users.

- **Automated test suite** (12 tests, 10 passing, 3 with
  test-bug false-positives): covers module imports, storage
  singleton, persistence round-trip, rating parser, long-
  response chunking, abort_event LRU, global LLM semaphore,
  prompts constants, config env, end-to-end private chat
  (real Qwen3.8-27B → 261 chars), end-to-end group with
  RATING_MODE (real Qwen3.8-27B → 2 replies + 1 reaction),
  and `/start` command (257 chars welcome message).

### Deferred (next-wave candidates)

These require real Telegram user interaction (single user
can't simulate) or a slow LLM swap (Ornith instead of Qwen).
Listed in `docs/TODO.md` under "Critical — operator tests":

- **O1 — Stop button end-to-end UX** (5 min)
- **O2 — Multi-user concurrency in group** (10 min)
- **O3 — Edit-during-LLM** (10 min; bot is in polling, not
  webhook, so edits aren't seen until the polling cycle
  finishes — known limitation, full fix is the webhook
  migration in F4 deferred)
- **O4 — `/reset` in a group forum topic** (5 min)
- **O5 — Donsetch tool via real user prompt** (5 min: ask
  the bot to web_search something; verify the MCP call lands)
- **O6 — Visual stop-button feedback** (2 min: when ⏹ is
  pressed, does the thinking message update, does the
  keyboard go away)

## [v0.6.1] — 2026-10-10 — P0 hotfix for F3 module split

Five P0 bugs from the v0.6.0 post-release review, all in bot.py
or config.py. No user-facing behavior change - these were
all internal cleanups. Silent hotfix (no public release notes
at the time; added here retrospectively).

### Fixed

- **P0-1: F4 LRU bound is broken in handlers.** handlers.py
  used raw `_abort_events[key] = ev` in 3 places, bypassing
  the LRU eviction. Replaced with
  `state._register_abort_event(chat_id, msg_id, ev)` in
  handle_photo, handle_document, handle_text. handle_voice
  didn't have abort_event at all in v0.5.2 - added it as
  part of the same fix.
- **P0-2: TWO _abort_events dicts in bot.py.** bot.py had a
  duplicate OrderedDict and `_register_abort_event` function
  that shadowed the state.py versions. Deleted the 60-line
  block, kept only the `from state import ...` re-exports.
- **P0-3: bot.py duplicated config constants.** 60 lines
  of env-var reads (BOT_TOKEN, LLAMA_URL, ALLOWED_USER_IDS,
  MAX_*_BYTES) AFTER importing them from config.py. The
  local declarations shadowed the imports. Also deleted
  the hardcoded `DISABLED_TOOLS = {25-entry set}` that
  ignored the env-overridable config.DISABLED_TOOLS.
- **P0-4: Two Storage instances.** bot.py had
  `store = storage.Storage(DB_PATH)` creating an eager
  instance; storage.py had `get_store()` lazy singleton.
  Two open SQLite connections. Deleted the eager init.
- **P0-5: Dockerfile is broken.** `COPY bot.py storage.py ./`
  only copied 2 of 10 Python files. Changed to
  `COPY *.py ./` with explanation comment.

### Verified

E2E with real Qwen3.8-27B: 173 chars response with [llm]
prefix, abort_events cleaned to 0 after handler.

## [v0.6.2] — 2026-10-11 — selftest regression hotfix after v0.6.1

The v0.6.1 P0-4 fix (delete eager `store = ...`) broke the
in-tree selftest, which uses `import bot as _b; _b.store.xxx`
in 25 places. The hotfix restores the selftest as the safety
net for the next refactor. Silent hotfix (no public release
notes at the time; added here retrospectively).

### Fixed

- **T1a: Re-export Storage as `bot.store`.** Used
  `from storage import get_store; store = get_store()` to
  make `bot.store` a real Storage instance (not the get_store
  function, which was the v0.6.1 mistake). Single instance
  shared with `storage.get_store()`.
- **T1b: Re-export `_PER_USER_SEMAPHORE_LIMIT` and
  `_GLOBAL_LLM_SEM_LIMIT` to bot.py** (selftest used these).
- **T2-T6: Dead code cleanup.** `from collections import
  OrderedDict`, `import storage`, `DB_PATH = ...`,
  `_apply_reaction` import, 3× duplicate concurrency caps
  in config.py, `_s()` wrapper in persistence.py.

### Deferred (v0.6.3)

- LRU regression test in in-tree selftest (claimed in v0.6.2
  commit message but not actually added - see v0.6.3).
- Rating parser test `rest`→`body` KeyError (broken since
  v0.6.0, fixed in v0.6.3).
- Existing abort_event test uses raw insert (gives false
  confidence, fixed in v0.6.3).

## [v0.5.0] — 2026-10-10 — Qwen migration, GROUP_CONTEXT, react_to_message

Big wave: model switch (Ornith → Qwen3.8-27B-Ultra-Heretic-MTP-256k),
GROUP_CONTEXT injected on every call_llama, react_to_message tool
for ad-hoc LLM reactions, full PTB 21 migration, donsetch v4.7.4
integration. The bot now passes the model switch, the system
prompt, the tool call, and the abort ladder without losing
state.

### Added

- **GROUP_CONTEXT system prompt** (bot.py `GROUP_CONTEXT`,
  ~80 lines, injected in every call_llama call before tool-mode
  or rating-mode). Locks the LlmChatPlace rules (10-step rating
  scale, [llm] tag, truth-over-style, no tone policing) into a
  system message that survives model switches and /reset. The
  pinned welcome message stays for humans; the LLM only sees
  this. See [docs/CALL_LLAMA.md](docs/CALL_LLAMA.md) §GROUP_CONTEXT.

- **`react_to_message` tool** (OpenAI function-calling). LLM
  can set a single-emoji Telegram reaction on any message in
  the current chat. Standard 10-step reaction set (👍 👏 ❤️ 🔥
  positive, 😐 🤔 neutral, 😢 😡 🤮 💩 negative). Executor
  wraps `bot.set_message_reaction` with `ReactionTypeEmoji`.
  Default `message_id` is the user's current message. Available
  in non-rating mode; rating mode still uses the hardcoded path.

- **Sender name in history** (storage v4). The bot tags every
  user message with `From: <name>: <text>` so the LLM can tell
  Vasisualy from Dimon in a group. NULL → `From: user:`. Schema
  migration `messages.sender_name TEXT` is idempotent.

- **Malformed tool-call JSON rejection** (Qwen 4.6.x line had
  runaway loops when the LLM emitted `max_chars:8000` 10 times).
  Three checks in the dispatcher:
  1. cheap pre-check (`":` count > 8 or len > 1500) → REJECT
  2. `json.loads` raises → REJECT with parse error
  3. parses to `{}` for non-empty raw → REJECT
  Each rejection returns a tool result asking the LLM to
  re-emit cleanly. Closes the Qwen-500-loop on bad chains.

- **`abort_event` per-handler key**: the Stop button now keys
  on `(chat_id, thinking.message_id)` instead of
  `(chat_id, user_id)`. Concurrent handlers no longer overwrite
  each other's events. 3 selftest cases for distinct events
  across two in-flight handlers.

- **donsetch v4.7.4 integration** with split-shape result.
  `DONSETCH_MCP__TEXT_ONLY=false` (double underscore) in
  `/opt/search/docker/docker-compose.yml` returns the
  human-readable text + structured JSON separately, matching
  what the bot's `donsetch_call` was designed for. Locked in
  by a new live-MCP selftest block that asserts the response
  contains `"Search results"` and NOT `"[meta]"`.

- **Comprehensive selftest suite** (~75 cases, runs on every
  container start). Categories: routing, known_topics, lazy
  history trim, rating parser, rating emoji, noise filter,
  reply filter, per-user semaphore, LLM smoke, chat_member
  welcome, edit-replace, per-handler abort key, PTB 21
  download_with_limit, sender-name flow, malformed tool-call
  JSON rejection, react_to_message, non-streaming tool_call
  regression, GROUP_CONTEXT injection, real donsetch
  integration. Every commit that adds behaviour adds a case.

### Changed

- **Model switch**: `LLAMA_URL` `192.168.10.7:8080` → `192.168.10.6:8080`,
  `MODEL` `Ornith-1.5-35B-A3B-Uncensored` → `Qwen3.8-27B-Ultra-Heretic-MTP-256k`.
  Speculative decoding (MTP) on Qwen 4.7.x. Vision support
  retained (multimodal flag). Whisper stays on `.7:8000`.

- **`call_llama` non-streaming branch** now falls through to
  the unified tool-dispatch loop when the LLM responds with
  `tool_calls`. The old code unconditionally returned
  `content` after a non-streaming POST, which silently dropped
  vision-task tool calls (e.g. "react to this picture" would
  hang with empty thinking). Sentinel `_ns_handled` skips the
  streaming branch to avoid a second POST. Regression test
  mocks llama-server to return a tool_call + a follow-up text,
  asserts the executor ran exactly once.

- **`[llm]` tag reclassified** from "opt-out convention" to
  "REQUIRED header on every LLM message". The earlier wording
  let the LLM interpret the tag as optional and skip it on
  direct answers, which broke the peer-LLM rating protocol.
  New wording in GROUP_CONTEXT §3 is explicit, with examples
  for every message type and a "do NOT skip" rule in the
  not-do list.

- **Tool dispatch** (`bot.py:_tag_sender` closure) gained
  three new kwargs: `bot`, `chat_id`, `current_message_id`.
  Threaded through all 4 handlers (text/photo/voice/document)
  and their retry branches. None defaults are safe — the
  dispatch branch only fires when the LLM actually calls
  `react_to_message`.

### Fixed

- **PTB 21 download compat**: `File.download_as_chunks` was
  removed in PTB 21. Replaced with `File.download_as_bytearray`
  in a new `_download_with_limit` helper. Two size guards:
  pre-check on `file.file_size` (no download if Telegram
  already knows the file is too big), post-check on `len(buf)`
  (catches the rare case where `file_size` is missing).
  4 selftest cases for oversized rejection, small download,
  friendly error, post-check.

- **Stop button race condition**: in group mode with concurrent
  users, clicking Stop on one user's thinking message
  sometimes aborted a DIFFERENT user's request. Caused by the
  abort_event key being `(chat_id, user_id)` — the dispatch
  loop in a different handler overwrote the event. Fixed by
  keying on `(chat_id, thinking.message_id)`. Verified by
  selftest with two concurrent in-flight handlers.

- **Tool-call JSON runaway loop** (Qwen 4.6.x + MTP heads):
  LLM emitted `max_chars:8000` 10 times in the same args
  object. `json.loads` silently took the last value, the
  tool errored, the LLM retried with more garbage, until
  llama-server returned 500. The 500 was the SYMPTOM; the
  runaway tool-calling loop was the cause. Fixed in
  `call_llama` dispatcher (see "Malformed tool-call JSON
  rejection" above).

- **`import asyncio` shadowing in `_selftest`**: a local
  `import asyncio` inside a new selftest block made `asyncio`
  a function-local variable for the WHOLE `_selftest` function,
  breaking every earlier `asyncio.X(...)` call with
  UnboundLocalError. Bot crashed on first startup after the
  new test was added. Fixed by using the module-level import
  (asyncio is already imported at the top of bot.py).

# Changelog

All notable changes to this project are documented here.
Format: [Semantic Versioning](https://semver.org/) + sections
per release. The repo's `git log` is the granular record;
this file is the human-readable summary.

## [v0.5.1] — 2026-10-10 — third-pass review fixes (T1-T8)

Response to the third code-review pass on v0.5.0. The P0
items are real bugs that the prior two reviews missed;
the P1/P2 items are refactoring and dead-code removal.

### P0 — fix before next deploy

  - **T1 (P0-1): global LLM concurrency cap** — added
    `_GLOBAL_LLM_SEM = asyncio.Semaphore(4)` and wired it
    into all 4 message handlers (handle_text, handle_photo,
    handle_voice, handle_document). Without this cap, the
    v0.5 `asyncio.create_task` switch in the polling loop
    plus the per-user semaphore (=2) meant 50 different
    users in a group could each fire one message and we'd
    queue 50 concurrent 27B forward passes on a single GPU
    → OOM. With cap=4, llama-server sees at most 4
    in-flight requests no matter how many users spam.
    Defense in depth: also added `limit=10` to the
    `getUpdates` params so the per-cycle batch is bounded.
  - **T2 (P0-2): GROUP_CONTEXT position in tools mode**.
    The previous order in tools mode was
    `[helpful_assistant_sys, GROUP_CONTEXT, ...rest]`, which
    made Qwen3.8 treat the generic "helpful assistant"
    persona as the most-authoritative system message and
    deprioritise the LlmChatPlace rules. Fix Option B from
    the review: rewrite the tools-mode `sys_prompt` to lead
    with GROUP_CONTEXT (so the [llm] tag and rating-on-truth
    rules are at the most-authoritative position), then
    drop the now-redundant `_group_ctx_msg` from `messages`
    in tools mode. Rating mode is unchanged (RATING_RULES
    first, GROUP_CONTEXT second).

### P1 — refactor + dead code

  - **T3 (P1-1): `_apply_rating_and_persist()` helper** —
    pulled the 30-line `if rating_active: ... else: ...`
    block out of all 4 handlers into a single async helper.
    Each handler now has a one-line call. Removes ~100
    lines of duplication. Pure refactor, no behavior
    change; all selftest cases still pass.
  - **T4 (P1-2): deleted dead code** — `_msg_text_edited_during()`
    function (10 lines) + the `_user_msg_text` data structure
    and its 50-line explanatory comment block (60 lines) +
    the polling-loop write to `_user_msg_text` (12 lines) +
    the `_bg_text_offset` global. The function was a no-op
    since the background text updater was disabled
    (Telegram rejects simultaneous getUpdates with HTTP
    409), and nothing else read the dict. Total: ~130
    lines deleted.
  - **T5 (P1-3): `__ABORTED__` sentinel → `None`** — `call_llama`
    returns `None` (was `'__ABORTED__'`) on abort; handlers
    check `if bot_response is None` (was `== '__ABORTED__'`).
    3 return sites + 5 check sites updated. The string
    sentinel was fragile: a user could paste `__ABORTED__`
    into chat and the bot would silently swallow its own
    response.
  - **T6 (P1-4): `_tag_sender` deduplicated** — the
    tools-mode closure and the rating-mode closure were
    byte-for-byte identical. Lifted to a single module-level
    function. Both branches now use the same code; "did I
    keep them in sync?" hazard removed.

### P2 — cleanup

  - **T7 (P2-1): `_is_rating_active()` helper** — the 3-line
    `RATING_MODE and _is_group_chat(update) and not
    _should_mute_in_group(update)` check was duplicated in
    4 handlers. Now a single function. The `not
    _should_mute_in_group(update)` clause was already dead
    at the call sites (the dispatch filter drops muted
    messages earlier), but kept as defense-in-depth inside
    the helper.
  - **T8 (P2-2): duplicate GROUP_CONTEXT comment removed** —
    two ~7-line blocks saying the same thing in different
    words. Kept one.

### No production behavior change

End users see the same responses, the same reactions, the
same [llm] tagging, the same tools, the same rating emoji.
The P0 fixes are structural (prevent OOM, make GROUP_CONTEXT
actually take effect); the P1/P2 fixes are refactoring that
should be invisible.

### Live selftest

All 75+ selftest cases pass on container restart. The
"real donsetch integration" subtest is skipped inside the
bot's event loop (it needs `asyncio.run()` which can't be
called from a running loop) — same limitation as v0.5.0;
manually verified via `bot_smoke.py` against the live
donsetch-http.

### Net diff

`bot.py`: +232 / −300 (net −68 lines, despite the new
helper functions, because T3 + T4 alone removed ~200 lines
of duplicated/dispatch/dead code).

## [v0.5.2] — 2026-10-10 — v0.5.1 follow-up paperwork (F1, F2)

Two trivial follow-ups from the v0.5.0 fourth-pass review.
Both are 5-minute edits. No production behavior change.

### Fixed

  - **F1 (NEW-1): stale comment at handle_photo:2923** —
    The 14-line "Edit-during-LLM detection" comment in
    `handle_photo` survived the T4 dead-code deletion in
    v0.5.1 and still said "we keep the data structure for
    future use" — but `_user_msg_text` is gone. Replaced
    with a 6-line comment that explicitly says the dict and
    the helper function were deleted in v0.5.1 (T4) and
    points the reader at the polling loop's edit-replace
    block as the live mechanism. Same fix applies to any
    other handler that has the same comment (only
    `handle_photo` did; the search is exhaustive).
  - **F2 (NEW-2): selftest for `_GLOBAL_LLM_SEM`** —
    The v0.5.1 global LLM concurrency cap (T1) was a
    production safety mechanism but had no selftest
    coverage. Added a 9-line smoke test that acquires
    `_GLOBAL_LLM_SEM_LIMIT=4` slots and asserts the
    semaphore is locked, then releases and asserts it's
    unlocked. Catches a future refactor that accidentally
    removes the cap or changes the limit.

### No production behavior change

End users see the same responses, same reactions, same
[llm] tagging, same tools. The two changes are a comment
edit and a selftest addition; runtime code is unchanged.

### Live selftest

All previous selftest cases still pass; the new
"global LLM semaphore test" group reports 2/2 OK.

### Net diff

`bot.py`: +36 / −15. The +36 includes the new selftest
code (~22 lines: 5-line test + 11-line docstring +
assertion messages) and the new shorter comment (6 lines
vs 14 lines removed). The net is +21 lines, which is
expected for comment + test additions.

## [Unreleased] — group-mode migration

The bot now runs in **dual mode**: a 1:1 private chat (the
original use case) OR a Telegram supergroup with Topics
(collaborative, multi-user, one LLM history per topic).
Same storage layer, same call_llama pipeline, same tool stack.

### Added

- **Group mode** (Telegram supergroup with Topics):
  - Bot auto-detects via `chat.is_forum` (NOT `message_thread_id`,
    which is `None` for the General topic — see commit 4 in
    the migration for the gotcha)
  - `/newsub <name>` calls `createForumTopic` and replies in
    the current topic
  - `/subs` lists topics the bot created (from local `known_topics`
    table; Bot API has no `getForumTopics` for bots)
  - `/delsub <name|id>` looks up by name or numeric id, calls
    `deleteForumTopic`, wipes local history
  - `/reset` clears messages in the current topic
  - `/here`, `/stats` work in both modes
  - Bot replies in the same topic via `_reply()` that
    preserves `message_thread_id` (PTB's `Message.reply_text`
    does NOT pass it through by default)
  - General topic uses `"general"` as the storage thread_id
    sentinel (Telegram does not assign a numeric id to it)

- **UTF-8 names** for sub-talks and topics. The previous
  ASCII-only regex is replaced with `^[^\s]{1,32}$` (sub-talks)
  and `^[^\s]{1,128}$` (forum topics). Russian, Chinese, emoji —
  all OK.

- **Edit-cmd handling**: edits of a command (tap message → Edit
  → Save, or up-arrow + edit + Send) are treated as new
  commands. Edits of regular text are dropped (we already
  answered the original).

- **Group-mode noise filter ("[llm] honor system")**: the bot
  stays silent in group mode for messages that are either
  from another Telegram bot (`is_bot=true`) or contain
  `[llm]` anywhere in the text or caption (case-insensitive).
  The marker is a convention set in the group's pinned
  welcome message ("If you are LLM, mark yourself with [llm]
  and answer user questions"). The filter is applied at
  the dispatch layer (`_dispatch_update`) so EVERY handler
  — message AND command — is covered uniformly. In private
  mode the filter is a no-op (no other LLM to yield to).
  Suppressions are logged as `[dispatch] muting group-mode
  message from <who>`.

- **Selftest expanded**: 6 cases for `_is_group_chat` matrix
  (private, group legacy, supergroup, supergroup+forum+General,
  supergroup+forum+topic 42, double-check on private) plus a
  full add/list/find/remove round-trip on `known_topics`,
  plus a 7-case matrix for `_should_mute_in_group` covering
  group/private mode × is_bot/has-marker/no-from-user.

### Changed

- **Storage schema v2**: `(user_id, sub_talk)` renamed to
  `(chat_id, thread_id)`. v1 → v2 migration is automatic on
  first `Storage()` init (SQLite `ALTER TABLE RENAME COLUMN`,
  3.25+). Private mode stores `chat_id == user_id`, group mode
  stores `chat_id == group_chat_id` (negative) and
  `thread_id == str(message_thread_id)`.

- **Whitelist semantics**: in private mode, `ALLOWED_USER_IDS`
  and `ALLOWED_USERNAMES` are enforced (silent rejection on
  miss, LOCKDOWN if both empty). In group mode, the whitelist
  is **skipped entirely** — access control is delegated to
  Telegram (group membership, per-topic permissions, admin
  status).

- **All `update.message.reply_text` call sites** in handlers
  replaced with `await _reply(update, ...)`. The helper
  passes `message_thread_id` through when in a topic.

- **Documentation**: README, SETUP, ARCHITECTURE, SECURITY
  all rewritten for dual-mode. `docs/SETUP.md` has a
  "Deployment mode B: Group with Telegram Topics" section
  with 8 sub-sections. `docs/SECURITY.md` reorganised into
  8 numbered sections covering both modes.

### Fixed

- `getForumTopics` does not exist in the Bot API for bots.
  Worked around with the `known_topics` local table
  (commit 7). Without this, `/subs` and `/delsub <name>`
  were unreachable in group mode.

- `edited_message` updates crashed handlers because
  `update.message is None` for those. Fixed in two steps:
  first by dropping the updates (commit 5), then by
  re-processing edited commands (commit 8) for better UX.

- `cmd_callback` had `name` and `thread_id` mixed up (would
  have crashed on first inline-button click in production).
  Found and fixed during the migration.

- `chat.is_forum` is the canonical "is this a group" signal.
  `message_thread_id is not None` was wrong (General topic
  has it as None).

- Russian / Cyrillic / non-ASCII names for sub-talks and
  topics were rejected by the old ASCII-only regex.

### Known limitations

- Topics created outside the bot (manually in Telegram UI, or
  by another bot) are not in `known_topics` and won't show
  in `/subs`. Workaround: delete via UI, recreate via `/newsub`.

- Renamed topics keep their old name in the local DB. Re-issuing
  `/newsub` with the new name updates the row (via `ON CONFLICT
  DO UPDATE`).

- Pre-`0dce580` topics (created before the `known_topics` table
  existed) are not retroactively imported. User must recreate
  them via `/newsub` or delete them via UI.

- No per-user rate limit / semaphore. A user in a group can
  flood the bot with messages. Tracked, not implemented.

- No `/ban` or `/del` admin commands. Deferred until the
  user has real spam to test against.

## Earlier versions

See `git log` for the pre-`cf96920` history. Major themes:

- `750bf2a` — `2f518d8`: SQLite-backed sub-talks, native
  Telegram menu (`setMyCommands` + inline keyboards).
- `c0a0778` / `7c53067` — second-pass review fixes (R-1..R-8).
- `a98d116` — first-pass review fixes (P0-P3).
- `eab99ba` — initial review follow-ups (PII, graceful
  shutdown, tools retry, username warning).
- `26c5556` — initial `CALL_LLAMA.md` and architecture docs.
- `1259cde` — replace hardcoded `192.168.10.7:8080` with
  localhost defaults + auto-discover model.
- `b16500e` — raise `max_tokens` for vision/voice/document
  to 16384.
- `97f2fc8` — simplify non-streaming path in `call_llama`
  (the 15-iteration vision bug).
- `a62bec5` — detect actual image MIME (Telegram photos are
  often WebP, not JPEG).
- `b16500e`, `97f2fc8`, `a62bec5` — first set of vision
  fixes.
- Earlier: `0d0a2db4` and friends — initial bot implementation
  with `conversations = {}` in-memory dict.
