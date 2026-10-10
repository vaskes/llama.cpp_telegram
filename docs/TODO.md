# TODO - Deferred tasks (post v0.5.2)

Backlog of work discussed but not yet scheduled. The current
v0.5.2 release closed the bot.py review cycle. This file
tracks what's NEXT once the user picks the next item.

Items are grouped by origin. Most come from the v0.5.0/v0.5.1
review waves (T1-T8, F1, F2 - all closed) and the follow-up
task list (F3-F8).

## Critical — v0.6.0 operator tests (run by Vasisualy tomorrow)

**Status:** Blocked on real-Telegram interaction. The automated
test suite (10/12 passing, 2 with test-bug false positives) covers
the bot's internal logic, but six scenarios require a human
operator (real UI click, real user message, multi-user session,
or a slower model swap). All of these existed in v0.5.2 with
the same expected behaviour — the F3 refactor is supposed to be
behaviour-preserving, so a regression here would be a real bug.

**Why these aren't automated:**
- O1, O6: need a real ⏹ click in the Telegram UI (or an
  inline-keyboard simulation that the bot library doesn't
  support cleanly).
- O2: needs 2+ distinct Telegram user sessions sending
  concurrent requests — the test bot is single-user.
- O3: needs a real `edited_message` update from the Telegram
  server, which only fires when a real user edits a message
  in the chat.
- O4: needs an update from a forum topic thread, which
  fires on the `is_forum=True` supergroup. The mocked tests
  skip the full dispatch path.
- O5: needs the LLM to actually choose the tool and pass
  real arguments. A scripted prompt is unreliable (the LLM
  might respond in text instead of calling the tool).

**Why O1–O5 need a slower model (Ornith):** Qwen3.8-27B
responds in <2s, which is too fast to click Stop or edit a
message between the request arriving and the response landing.
The user can switch back to Ornith (which takes 15-30s per
response) by editing the docker-compose.yml to use
`MODEL=Ornith-1.5-35B-A3B-Uncensored` and restarting. After
the tests, switch back to Qwen.

### O1 — Stop button end-to-end UX (5 min, needs Ornith)

Switch to Ornith, then in `LlmChatPlace/ChitChat`:
1. Send a long prompt: "Расскажи подробно про Ренессанс,
   минимум 3 абзаца"
2. As soon as the 💭 thinking message appears with the ⏹
   button, click it.
3. Expected: thinking message edits to "⏹ Остановлено",
   no final response text arrives, `abort_event` log line
   in `docker logs telegram-bot`.
4. Then send the prompt again and let it finish — make sure
   normal flow still works.

**Acceptance:** Stop click produces "⏹ Остановлено" within
2s, and a fresh request right after completes normally.

### O2 — Multi-user concurrency in group (10 min, can use Qwen)

Needs Dmitri (id 117382588) to participate, or anyone
else in `ALLOWED_USER_IDS`. Currently only Vasisualy is
active. In `LlmChatPlace/ChitChat`:
1. Vasisualy sends: "Расскажи про Python asyncio в деталях"
2. While Vasisualy's request is in flight, Dmitri sends:
   "А ты что думаешь про Rust?"
3. Expected: both messages get answered, no slot leak, both
   reasoning streams visible in the chat.
4. Run `docker logs telegram-bot | grep -E "user_semaphore"`
   to verify no slot leak warnings.

**Acceptance:** both messages answered, no "Бот уже
обрабатывает N твоих запросов" reply, `docker logs` shows
`sem.release()` for each acquire.

### O3 — Edit-during-LLM (10 min, needs Ornith)

Switch to Ornith, then in `LlmChatPlace/ChitChat`:
1. Send: "Расскажи про Ренессанс"
2. While 💭 thinking is visible (15-30s window with Ornith),
   edit the message: change the text to "Расскажи про
   барокко"
3. Expected: bot does NOT crash. The first response arrives
   (the original "про Ренессанс" answer). The edit is NOT
   reprocessed by the bot in real time — by design, the
   bot is in polling mode, not webhook, so the edit is
   seen on the NEXT poll cycle. If the edit is older than
   the abort ladder checks, you may see a duplicate
   response (one for original, one for edit). Both
   behaviours are acceptable; the bot MUST NOT crash.

**Known limitation:** full edit-during-LLM fix requires
webhook mode (F4 deferred). Polling sees the edit at the
next `getUpdates` call (3-5s), by which time the original
LLM call is already returning.

**Acceptance:** no crash, no "Error" reply, original
response arrives. Edit reprocessing is best-effort.

### O4 — `/reset` in a group forum topic (5 min, can use Qwen)

In `LlmChatPlace/TechIssues` (thread_id 25):
1. Send a few messages to build up history:
   "test 1", "test 2", "test 3"
2. Send `/reset`
3. Expected: bot replies with something like "🧹 Sub-talk
   cleared" (or the v0.5.2 equivalent).
4. Send "test 4" — the LLM should NOT see test 1/2/3 in
   the context anymore.

**Acceptance:** post-reset history starts fresh. Verify
via `docker logs telegram-bot | grep -E "history_len"`:
the line after the reset should show history_len=1 (just
the new "test 4" message).

### O5 — Donsetch tool via real user prompt (5 min, can use Qwen)

In `LlmChatPlace/ChitChat` (private mode also works but
group is the realistic scenario):
1. Send: "Найди в интернете последние новости про Qwen3"
2. Expected: bot's reasoning shows it calling
   `donsetch_web_search`, then synthesises a reply with
   the search results. The response should include real
   URLs from the search.
3. Verify: response contains at least one URL or search-
   result snippet. Check `docker logs telegram-bot | grep
   "donsetch"` for the MCP session log lines.

**Acceptance:** tool was called, response is grounded in
real search results (not a hallucinated answer). If the
LLM skips the tool and answers from memory, retry with a
more direct prompt: "Use the web_search tool to find ..."

### O6 — Visual stop-button feedback (2 min, needs Ornith)

Switch to Ornith, then in `LlmChatPlace/ChitChat`:
1. Send: "Расскажи про Ренессанс, очень подробно"
2. While 💭 thinking is visible, observe the ⏹ button
   rendering (it should be at the bottom of the message,
   full width or icon-sized depending on Telegram client).
3. Click ⏹ and observe the inline keyboard behaviour:
   does the button disappear? Does the thinking message
   text update? How fast is the visual feedback (sub-2s
   is the goal)?

**Acceptance:** button renders correctly on initial
display; on click, the thinking message text changes
within 2s and the inline keyboard goes away.

### Reporting

After running the operator tests, update this section with
✅/❌ per test. Any ❌ becomes a v0.6.1 hotfix.

---

## Active (next-wave candidates)

### F3 - Module split of `telegram-bot/bot.py` - DONE

**Status:** Complete on `feature/f3-module-split` (commit
chain `ac0f319`..`c95b683`, pushed to origin 2026-10-11).
**What:** Split the 5 518-line `bot.py` into 7 modules:
- `config.py` (86 lines) - env constants, async primitives,
  concurrency caps
- `prompts.py` (133) - GROUP_CONTEXT, RATING_RULES,
  WELCOME_TEXT, regex, RATING_EMOJI, BLOAT_EMOJI
- `persistence.py` (76) - persist(), load_history()
- `rating.py` (203) - rating-mode parser + applier +
  react-to-message helper
- `call_llama.py` (1142) - LLM call layer: tool defs,
  donsetch MCP client, sender-name tagging, call_llama(),
  transcribe_voice, fetch_tools_from_llama
- `handlers.py` (757) - the 4 message handlers (text/photo/
  voice/document) + their helpers
- `dispatch.py` (1411) - main(), polling, command handlers,
  auth, group routing, concurrency helpers, _dispatch_update
- `bot.py` (2151) - imports, re-exports, _selftest, __main__

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

### F4 - Bounded `_abort_events` cleanup - DONE in v0.6.0

**Status:** DONE in v0.6.0. See commits `6985249` (initial
implementation) and `935f4d2` (handlers.py migration).
- `_abort_events` is `collections.OrderedDict` with
  `_ABORT_EVENTS_MAXSIZE = 200`.
- `_register_abort_event(chat_id, msg_id, ev)` does
  `move_to_end` on touch and `popitem(last=False)` to evict
  the oldest entry at cap.
- All 4 message handlers (photo, voice, document, text)
  use `_register_abort_event` since v0.6.1.
- In-tree selftest has F4 LRU regression test (3-line
  insert-250-assert-200) since v0.6.3.


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
