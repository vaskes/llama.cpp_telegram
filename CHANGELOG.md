# Changelog

All notable changes to this project are documented here.
Format: [Semantic Versioning](https://semver.org/) + sections
per release. The repo's `git log` is the granular record;
this file is the human-readable summary.

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
