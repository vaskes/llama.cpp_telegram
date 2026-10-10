# Third-pass review of v0.5.0 (2026-10-10)

**Repo:** https://github.com/vaskes/llama.cpp_telegram
**Tag reviewed:** v0.5.0 (`81a89fa`)
**Reviewer:** Mavis (third wave)
**Fix release:** v0.5.1

## What v0.4 got right (acknowledged)

The v0.4 fixes (P0 trim, P1 WAL verify, P1 `/here` topic
name, P1 `/delsub` lookup, P1 PII-safe logs) are all in.
The P0-2 `_group_mode` global is gone — `grep` confirms
zero references. P1-1's WAL verification (`storage.py:237`)
catches the silent-fallback case. P1-2's `find_known_topic_by_id`
is wired into `cmd_here`. P1-3's `/delsub <id>` is a direct
SQL query. P1-4's polling loop now logs `text_hash=sha1[:10]`
instead of the full text.

That is a complete turnaround on the v0.4 list.

## P0 — must fix before next deploy (real bugs the prior reviews missed)

### P0-1: No global concurrency cap → llama-server OOM

The v0.5 `asyncio.create_task` switch in the polling loop
plus the per-user semaphore (=2) means: 50 different users
sending one message each = 50 concurrent 27B forward
passes on a single GPU. Each holds real VRAM and KV-cache.
Qwen3.8-27B with MTP would OOM before the wall-clock
budget fires.

**Fix (v0.5.1):** added `_GLOBAL_LLM_SEM =
asyncio.Semaphore(4)` and wired into all 4 message
handlers. Also `limit=10` on `getUpdates` as defense in
depth. With this, llama-server sees ≤4 in-flight requests
no matter how many users spam.

### P0-2: GROUP_CONTEXT undercut by tools-mode sys_prompt

The intent of `GROUP_CONTEXT` was that the LlmChatPlace
rules are the LLM's actual policy. The implementation put
them at `messages[0]` (the second system message in
tools mode), but the tools-mode branch prepends a
"helpful assistant with tools" sys_prompt, making the
order `[helpful_assistant, GROUP_CONTEXT, ...rest]`. Qwen
(and most models) treat the first system message as
most authoritative → the LlmChatPlace rules were
deprioritised. The `[llm] tag REQUIRED` instruction at
`bot.py:2172` is exactly the kind of instruction that gets
lost in this situation.

**Fix (v0.5.1):** rewrote the tools-mode `sys_prompt` to
lead with `GROUP_CONTEXT`, then the tool guidance, and
dropped the now-redundant `_group_ctx_msg` from `messages`
in tools mode. Rating mode is unchanged (RATING_RULES
first, GROUP_CONTEXT second — that ordering was correct).

## P1 — refactor + dead code

### P1-1: RATING_MODE dispatch copy-pasted 4×

The 30-line `if rating_active: ... else: ...` block was
duplicated in all 4 message handlers. Lifted into
`_apply_rating_and_persist()` helper. Each handler
becomes a one-line call. Pure refactor, no behavior
change, all selftest cases pass.

### P1-2: Dead `_msg_text_edited_during` + `_user_msg_text`

Both are no-ops since the background text updater was
disabled (Telegram rejects simultaneous `getUpdates` with
HTTP 409). Deleted: the function (10 lines), the data
structure declaration (60 lines of comment + 1 line),
the polling-loop write (12 lines), and the
`_bg_text_offset` global.

### P1-3: `__ABORTED__` string sentinel

`call_llama` returned the literal string `'__ABORTED__'`
on abort. A user could paste that exact string into chat
and the bot would silently swallow its own response.
Changed to `return None`; handlers check
`if bot_response is None`.

### P1-4: `_tag_sender` and `_tag_sender_rating` duplicated

The two closures were byte-for-byte identical. Lifted to
a single module-level function. Both branches now share.

## P2 — cleanup

### P2-1: `rating_active` computed 4×

Same 3-line `RATING_MODE and _is_group_chat(update) and
not _should_mute_in_group(update)` check in 4 handlers.
Lifted to `_is_rating_active()` helper.

### P2-2: Duplicate GROUP_CONTEXT comment

Two ~7-line blocks saying the same thing in different
words. Kept one.

## What v0.4 review got right and v0.5 confirmed

- The "per-user semaphore will be needed for group mode"
  prediction became a real implementation in v0.5. The
  implementation is half of what is needed (per-user is
  necessary, not sufficient) — this review closes the
  other half.
- The "edit-during-LLM detection" feature was correctly
  identified as needing webhook support, and the team
  kept the data structure for that future migration. The
  v0.5.1 review confirms the data structure is no longer
  needed and removes it.
- The "synchronous=NORMAL needs documentation" P2-2 became
  a one-line comment in storage.
- The "WAL may fall back" P1-1 became an explicit
  verification PRAGMA.

The two things v0.4 missed:

- That `asyncio.create_task` would be needed for the Stop
  button to work — the v0.4 review was about the keying,
  not the dispatch. v0.5 had to make both changes at once
  because the keying fix didn't work without the dispatch
  change.
- That the per-user cap creates a global cap illusion.
  v0.4 explicitly said "per-user semaphore if the whitelist
  grows" — but the cap on per-user concurrency is
  necessary, not sufficient, once `asyncio.create_task` is
  in the mix. v0.5.1 closes this gap.

## Cross-check against user/project context

- **Memory rule: license is Kuduza Ai Lab, not personal.**
  Confirmed: `LICENSE` correctly attributes Kuduza Ai Lab.
- **Memory rule: `--mcp-servers-config` is the wiring
  point, not the shim.** Not touched in this review
  (it is a search-side concern).
- **Memory rule: `kanban-agent` uses real local HTTP test
  server, not mocks.** Confirmed: bot selftest is the same
  philosophy (subprocess-driven, against the live llama
  server, skipped if env is wrong).
- **Qwen 256k ctx preference** Confirmed: model is
  Qwen3.8-27B-Ultra-Heretic-MTP-256k on .6:8080. 256k
  context is comfortable for the typical 20-message window.

## Net

Code quality is high, documentation is honest, the third
review pass on the same codebase is finding things the
prior passes did not catch. The structural concern
(bot.py doubled in size, GROUP_CONTEXT persona conflict,
missing global cap) is the kind of thing that compounds
— each new feature that touches `call_llama` or the
handler dispatch will be harder to land correctly while
these are open.

**Three things fixed in v0.5.1:**
1. Global LLM concurrency cap (P0-1) — 4-line fix, prevents
   llama-server OOM.
2. GROUP_CONTEXT position in tools mode (P0-2) — 5-line fix,
   makes the LlmChatPlace rules actually take effect.
3. The `__ABORTED__` sentinel (P1-3) — 2-line fix, removes
   a control-flow fragility.

After that, the next wave is open territory: a v4 review
doc would be a good idea, and the 5 565-line bot.py is
begging for a module split (P2-5 deferred to a future
release).

## Known limitations (tracked, not in this release)

- **Webhook migration for edit-during-LLM real-time abort**
  (deferred — `bot.py` is in polling mode for now)
- **pytest + CI** (deferred — only `lint` workflow exists)
- **Module split of bot.py** (P2-5; deferred — T10)
- **Dmitriklim: hide reasoning stream from other group
  users** (new feature, not started)
- **Donsetch MCP_AUTH forwarding for non-loopback binds**
  (out-of-scope for this repo, mentioned in
  `DONSETCH_DEPLOY.md` "what you can break" section)
