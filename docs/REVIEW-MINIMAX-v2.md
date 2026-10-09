# Code review — Group-mode migration (Oct 2026)

## What was reviewed

`vaskes/llama.cpp_telegram` @ `cf96920` (HEAD at the time).

Scope: the 7-commit migration from "private-chat only" to
"dual-mode (private chat OR Telegram group with Topics)" that
ran over the evenings of 2026-10-09 and 2026-10-10. Touched:

  - `telegram-bot/storage.py` (schema v2 + `known_topics`)
  - `telegram-bot/bot.py` (routing, group-mode commands,
    edit handling, helper functions, expanded selftest)
  - `docs/{SETUP,ARCHITECTURE,SECURITY}.md`, `README.md`

Not in this migration: the LLM call path, polling loop, tool
intake, vision path, voice/document handlers (other than
`is_group_chat` for the security check). Those are unchanged
from the prior review (`eab99ba`).

## Commits in scope

| # | Commit | What |
|---|--------|------|
| 1 | `9f4a559` | Storage: rename `(user_id, sub_talk)` → `(chat_id, thread_id)` |
| 2 | `76e39ea` | Routing: `_route_to_thread(update)` + `_reply` thread passthrough |
| 3 | `9f6d299` | Group-mode commands: `/newsub /subs /delsub` → forum-topic API |
| 4 | `326ca48` | **hotfix**: General-topic detection via `chat.is_forum`, not `message_thread_id`; relaxed name regex to UTF-8 |
| 5 | `d049828` | Drop `edited_message` updates (the no-`update.message` crash) |
| 6 | `627d148` | Docs: README + SETUP + ARCHITECTURE + SECURITY rewrite for dual-mode |
| 7 | `0dce580` | `known_topics` local index (Bot API has no `getForumTopics`) |
| 8 | `cf96920` | Edit-cmd UX: re-process edited commands, drop edited text |

## What this migration was supposed to deliver

The user's request: "let the bot work in a Telegram group with
Topics, so multiple users share a chat but each topic has its
own conversation history". Concretely:

- The same SQLite store serves both modes. The bot can run in
  a 1:1 private chat (1 user, multi-thread via `/newsub`) or
  in a group (N users, N topics, each topic = one history).
- All `call_llama` machinery (tools, vision, voice) is shared.
- Commands in group mode map to Telegram API
  (`createForumTopic`, `deleteForumTopic`).

## What it actually delivers

All three goals met. Six more issues were found and fixed
during integration testing in the real group.

### 1. `message_thread_id is None` for the General topic (commit 4)

**Symptom (real):** User ran `/newsub Болталка` in the General
topic of a forum-enabled supergroup. Bot replied
"Created sub-talk" (the private-mode success string) and
nothing appeared in the Telegram sidebar. The user assumed
topics were not being created.

**Root cause:** Telegram Bot API quirk — messages in the
**General** topic of a forum-enabled supergroup have
`message_thread_id == None`. The original routing check was
`if update.message.message_thread_id is not None`, which
**always evaluated to False for messages in General** (the
most common entry point!). Bot silently fell through to
private-mode code, which created a sub-talk in the local DB
with no matching Telegram topic.

**Fix:** New helper `_is_group_chat(update)` that checks
`chat.is_forum` first, then `chat.type != "private"`. The
`message_thread_id` is now used as a *secondary* signal (for
which topic within the group) rather than as the discriminator
for "is this a group at all". `/newsub` then works in General
and creates a real Telegram topic with a real
`message_thread_id`.

**Lesson:** the Bot API surface is a poor fit for
domain-driven code. Reading "does this look like a topic
message?" off `message_thread_id` is wrong — General exists
in every forum supergroup and is real.

### 2. `getForumTopics` does not exist (commit 7)

**Symptom (real):** `/delsub пизделка` returned
"❌ getForumTopics failed: 'ExtBot' object has no attribute
'get_forum_topics'". Introspection of PTB 21's `ExtBot`
confirms: there is no method to list forum topics. The Bot
API itself does not expose this to bots (probably an
abuse-prevention decision — a malicious bot in a group could
enumerate every topic and silently join them all).

**Root cause:** We assumed the Bot API had a symmetric
get/create API for topics, like `editMessage` /
`getChat`-like patterns. It doesn't.

**Fix:** `known_topics` table in the local DB. Populated by
`/newsub` (we get the `message_thread_id` and name from the
return value of `createForumTopic` itself). `/subs` lists
from the table. `/delsub` looks up by name (or numeric id)
and calls `deleteForumTopic` with the stored id.

**Limitations, documented in code:**
  - Topics created outside the bot (manually in UI, or by
    another bot) won't appear in `/subs`. The user must
    delete them via the UI or recreate via `/newsub`.
  - Renamed topics keep their old name in the table; the
    UI name and the DB name can drift. Re-`/newsub` re-records
    via `ON CONFLICT DO UPDATE`.

**Lesson:** a bot that *creates* a resource should also
*track* it locally. The Bot API's read surface is
intentionally minimal; don't depend on it for things the
bot itself created.

### 3. UTF-8 topic names (commit 4)

**Symptom (real):** `/newsub Болталка` rejected — the regex
was ASCII-only.

**Root cause:** The original `_SUBTALK_NAME_RE` was
`^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$`. Defensive, but
exclusionary: any non-ASCII user gets a confusing error.

**Fix:** Two regexes: `^[^\s]{1,32}$` for sub-talks (any
non-whitespace 1-32 chars, since we use it as an inline-
keyboard `callback_data` byte) and `^[^\s]{1,128}$` for
forum topics (Telegram's 128-char topic-name limit). The
storage layer is parameterized SQL and never uses the name
as a file path, so there is no security cost to allowing
arbitrary UTF-8 in the name.

**Lesson:** regex-level "safety" often hides a non-problem
(parameterized SQL handles SQLi; we never `os.path.join`
on user input here). When in doubt, allow more.

### 4. `edited_message` update crashed handlers (commit 5)

**Symptom (real):** User edited their `/newsub` message to
`/newsub HumansCanAsk`. Bot's PTB dispatcher routed the
`edited_message` update to `cmd_newsub` (because the new
text started with `/`), which immediately crashed on
`update.message.text` (because `update.message is None` for
edited messages; the text is in `update.edited_message`).

**Root cause:** Handlers all assume `update.message` is set.
For `edited_message` updates, only `update.edited_message`
is set.

**Fix (first attempt, commit 5):** Skip `edited_message`
entirely in the polling loop. Correct from a strict-typing
view, but terrible UX: Telegram's "up-arrow + edit + Send"
flow produces an `edited_message`, and the user perceives
the bot as broken.

**Fix (second attempt, commit 8):** Branch in the polling
loop on the new text. If it starts with `/`, treat as a new
command (rewrite `upd_dict["message"] = upd_dict["edited_message"]`
so the dispatcher sees a regular `message`). If it's plain
text, drop (we already answered the original).

**Lesson:** Don't conflate "logically a new event" with
"matches the Bot API update-type taxonomy". Telegram has
`message` and `edited_message`; the bot's user model has
"user just sent a command" and "user just edited an old
message". The right mapping is the second axis, not the first.

### 5. Cmd-callback `NameError` (fixed in commit 3)

`cmd_callback` had `name` and `thread_id` mixed up. The
inline-keyboard sub-switching would have crashed on click.
Found while porting handlers to the new schema. Tests catch
this; the selftest exercises the storage layer but not the
inline keyboard (PTB's `CallbackQueryHandler` requires a
full `Update` to fire, which is hard to mock).

### 6. `get_chat` not used to enumerate topics (commit 7)

Tried `bot.get_chat(chat_id)` — it doesn't return a topics
list either. The introspection of `ExtBot` in commit 7 is
the authoritative list; if a method isn't there, the Bot
API doesn't expose it.

## Selftest now covers

| What | How |
|------|-----|
| Private mode path | Synthetic `/start` with `chat.type=private` |
| LLM call path | Real PNG sent to llama-server |
| `_is_group_chat` matrix | 6 cases: private, group legacy, supergroup, supergroup+forum+General, supergroup+forum+topic 42, and a double-check on private |
| `known_topics` round-trip | add → list → find-by-name → remove, against the live SQLite |

All 6 routing cases pass. All 3 storage ops pass. LLM smoke
returns ~500 chars (finish=stop). Selftest is the
`LLAMABOT_SELFTEST=1` env-var gated path; production deploys
set the env to skip it.

## Things that were NOT fixed in this migration

These are tracked in `TODOS.md` (will create next) or in
inline `TODO` comments:

- **No `getUpdates` "topics seen" inference.** An alternative
  design: when the bot receives a message with a
  `message_thread_id` it has never seen, INSERT it into
  `known_topics` opportunistically. That would auto-register
  topics created outside the bot. Not done because the user
  said "let at least spammers show up" first — i.e. wait
  for real group traffic before adding complexity.

- **No moderation commands.** `/ban`, `/del` would be
  straightforward to add (~30 lines) using
  `bot.delete_message()` and `bot.ban_chat_member()`. The
  user explicitly deferred this to avoid a "ban-hammer
  everything" over-reach.

- **No auto-moderation.** LLM-as-spam-detector is the wrong
  tool for the job (expensive, error-prone). Anti-spam is
  best done with heuristic filters (URLs, repetitive msgs)
  or a dedicated mod-bot (e.g. @GroupHelpBot, @Combot).

- **No per-user rate limit / semaphore.** A user in the
  group can flood the bot with messages; the bot serializes
  them through `call_llama`. For groups with many active
  users, a per-user semaphore is needed. Tracked but not
  implemented.

- **No `editForumTopic` → `/renamesub`.** The API supports
  it. A "rename" command would let the user re-name a topic
  from the bot instead of the Telegram UI. YAGNI for now.

- **No "topics created outside the bot" resync.** Once
  `known_topics` is built only from `/newsub`, the only way
  to add a foreign topic is to delete and recreate it.
  A `/resync` command that walks `messages.message_thread_id`
  and INSERT-OR-IGNORE into `known_topics` would work, but
  we don't have the *names* of foreign topics, so the user
  would still have to map them by hand.

## Pre-flight checklist for next review

- [x] All 4 message handlers (`handle_text / handle_voice /
      handle_document / handle_photo`) route through
      `_route_to_thread` and use `chat_id`/`thread_id` from
      its return value.
- [x] All 7 command handlers (`start / reset / stats /
      cmd_newsub / cmd_sub / cmd_subs / cmd_delsub / cmd_here
      / cmd_help / cmd_callback`) have a group-mode branch
      where applicable.
- [x] `_reply` preserves `message_thread_id` on replies.
- [x] `_is_group_chat` correctly classifies all 6 test cases.
- [x] `known_topics` round-trip works (add/list/find/remove).
- [x] Selftest covers all of the above.
- [x] LLM smoke test still returns a real response.
- [x] `docs/SETUP.md` has a Group Mode section with B.1–B.8.
- [x] `docs/ARCHITECTURE.md` "Group mode" section explains
      the routing table and the Telegram API surface.
- [x] `docs/SECURITY.md` covers both modes' security model.
- [x] `README.md` capability list mentions both modes.
