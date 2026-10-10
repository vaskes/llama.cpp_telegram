# Changelog

All notable changes to this project are documented here.
Format: [Semantic Versioning](https://semver.org/) + sections
per release. The repo's `git log` is the granular record;
this file is the human-readable summary.

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
