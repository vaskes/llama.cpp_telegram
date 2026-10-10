# TODO - Deferred tasks (post v0.5.2)

Backlog of work discussed but not yet scheduled. The current
v0.5.2 release closed the bot.py review cycle. This file
tracks what's NEXT once the user picks the next item.

Items are grouped by origin. Most come from the v0.5.0/v0.5.1
review waves (T1-T8, F1, F2 - all closed) and the follow-up
task list (F3-F8).

## Active (next-wave candidates)

### F3 - Module split of `telegram-bot/bot.py` - IN PROGRESS

**Status:** In progress on `feature/f3-module-split` worktree.
**What:** Split the 5 518-line `bot.py` into 6 modules:
- `prompts.py` (~150 lines) - GROUP_CONTEXT, RATING_RULES,
  WELCOME_TEXT, _TOOL_KEYWORDS, RATING_EMOJI, BLOAT_EMOJI
- `rating.py` (~120) - _parse_rating_response, _apply_reaction,
  _execute_react_to_message, _is_rating_active,
  _apply_rating_and_persist
- `handlers.py` (~600) - handle_text, handle_photo, handle_voice,
  handle_document, _download_with_limit, _sender_display_name
- `dispatch.py` (~400) - main(), _run(), _register_bot_menu,
  _dispatch_update, _handle_chat_member_update, polling loop
- `call_llama.py` (~900) - call_llama, _tools cache, tool defs
- `bot.py` (~3 200) - config, lifecycle, selftest

**Why:** Each new feature wave compounds review cost; a
6-module bot is reviewed ~10x cheaper than a 5 500-line
file.
**Order:** refactor -> unit tests (cover each new module) ->
review (third-party or self) -> optimizations on top of
clean structure.
**Estimated:** 4-6 hours of focused work, plus 1-2 hours
of test coverage.
**Webhook-readiness:** The `dispatch.py` module is designed
to be the natural place to swap polling for webhook mode
when the next migration happens.

### F4 - Bounded `_abort_events` cleanup - 30 min

**Status:** Deferred (P2).
**What:** The `_abort_events: dict` in `bot.py:3679` can
leak entries if a handler raises between insert and pop
without going through the `finally`. Per-user semaphore
bounds the leak but does not eliminate it.
**Fix options:** (a) `weakref.finalize` on the thinking
message; (b) per-call `ContextVar`; (c) explicit LRU cap
on the dict (e.g. maxsize=200 with oldest-first eviction).
**When:** Anytime after F3 lands. Easy 30-min cleanup.

### Webhook migration for edit-during-LLM abort - 6-10 h

**Status:** Deferred, P1-ish UX gap.
**What:** Bot is in long-polling mode. Cannot detect
"user edited their message while the bot is responding"
until the polling cycle completes. Current workaround:
polling loop's edit-replace block deletes the wrong answer
and re-processes the edit as a new user message - user
sees a brief wrong-answer window.
**Fix:** Switch to webhook mode (HTTPS endpoint on llmhost2;
Telegram POSTs updates instantly). In async handler for
the thinking request, see the edit "live" and either abort
the current LLM call (via the existing per-handler
`abort_event` from v0.5.1 T5) and start a new one, or mark
the in-flight answer as stale and skip sending.
**Infrastructure required:** TLS cert + domain or
Cloudflare tunnel on llmhost2; nginx reverse proxy;
`setWebhook` call on startup; rework of `_dispatch_update`
to accept the update directly instead of polling.
**Designed-for-future:** F3 module split sets up
`dispatch.py` to make this transition clean (one file to
rewrite, not 5 500 lines of `bot.py`).
**When:** When Vasisualy starts hitting the typo-double-
answer case frequently. Qwen3.8 takes 5-10s per answer so
the gap is visible now.

## P3 backlog (low priority, by request)

### F5 - `/ban` and `/del` admin commands - 30 min

**What:** Two owner-only commands. `/ban <user_id_or_reply>`
adds to `banned_users` table; bot drops all messages from
that user at dispatch. `/del <message_id>` deletes the
bot's reply (or anyone's, if PTB permissions allow).
**Why:** Currently the only escape for spammers is
Telegram-native ban, which is heavyweight for a single
operator bot.
**When:** When a real spammer shows up. Not preemptive.

### F6 - Opportunistic INSERT for foreign topics - 10 lines

**What:** When a message arrives with `message_thread_id`
not in `known_topics`, INSERT-OR-IGNORE with
name=`"(unknown)"` instead of dropping or routing to
General. User can `/newsub` later to give it a real name.
**Why:** Currently the bot is silent on a topic the moment
after creation until the operator remembers to `/newsub`.
**When:** When the operator starts creating topics
frequently.

### F7 - Qwen version-specific malformed-JSON threshold - 15 min

**What:** Current threshold `count(":") > 8 or len > 1500`
is tuned for Qwen-4.6.x MTP. Qwen-4.7.x may legitimately
emit 8+ fields.
**Why:** Defensive against the Qwen runaway tool-call
loop that hits llama-server's 500. If the threshold
becomes a false-positive on 4.7.x, we'd reject valid
tool calls.
**When:** When Qwen-4.7.x or donsetch upstream change
shape. Revisit on first report of false-positive
rejections.

### F8 - Content-based dedup instead of `is not _group_ctx_msg` - 10 min

**What:** `bot.py` (post-F3) has
`messages = [m for m in messages if m is not _group_ctx_msg]`
in the tools-mode branch (from v0.5.1 T2). Works because
`_group_ctx_msg` is a stable reference. If a future
refactor rebinds the variable, the filter breaks silently.
**Fix:** Content-based check -
`m.get("role") == "system" and m.get("content") == GROUP_CONTEXT and i > 0`.
**When:** When touching that branch for any reason.
Pure defensive coding.

## Future / "next after next" considerations

- **pytest + CI** (currently only `lint` workflow). F3 +
  unit tests unlocks pytest; a CI step that runs pytest
  on every push is the natural follow-up.
- **Caching for call_llama** - if Vasisualy starts asking
  the same questions repeatedly, a 5-min in-memory LRU
  cache for identical `(messages_hash, max_tokens)` keys
  could cut Qwen load significantly. NOT preemptive.
- **Streaming `react_to_message` mid-reasoning** - let
  the LLM emit reactions as it thinks (e.g. "👍" on a
  fact it agrees with). Currently reactions are
  post-answer. Out of scope.

## Closed in v0.5.x

See `CHANGELOG.md` and `docs/REVIEW-MINIMAX-v3.md` for the
T1-T8 (v0.5.0 third-pass review), F1/F2 (v0.5.1 fourth-pass
review) work. The review cycle on `bot.py` is currently
self-completing in a healthy way - 11/11 review items
addressed, all green.
