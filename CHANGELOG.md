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

## [v0.9.0] — 2026-10-11 — T25 webhook migration (sub-second latency)

Long-polling → webhook transport via aiohttp server + nginx
reverse-proxy. Production rollout at gw1.kuduza.com (Amsterdam
DC) with public hostname `bot.kuduza.com`. Single-port
ingress, secret-token authentication, drop-in TLS
termination at nginx. Falls back to long-polling by setting
`WEBHOOK_MODE=0`.

The migration touched every layer (network, secrets,
process supervision, deploy packaging), so it was released
as a major version bump rather than a hotfix.

### Added

- **`webhook.py`** (119 lines) — aiohttp server module.
  `make_app()` builds the app with two routes:
  `POST /tg-webhook/{tail:.*}` (verifies
  `X-Telegram-Bot-Api-Secret-Token` via constant-time
  `hmac.compare_digest`, then `asyncio.create_task` to
  `_dispatch_update()` and returns 200 immediately — fire-
  and-forget, since Telegram expects 200 within ~5s and
  LLM calls routinely take 30s+); and `GET /healthz` for
  operator liveness probes.

- **`dispatch.py` main branch** — when `WEBHOOK_MODE=1`,
  starts aiohttp server on `0.0.0.0:WEBHOOK_PORT`
  (default 8443) via `web.AppRunner` + `web.TCPSite` and
  blocks on `asyncio.Event().wait()`. When `WEBHOOK_MODE=0`,
  the original long-poll loop runs unchanged. The split is
  a single `if/else` at the top of `_run()`; everything
  below it (handlers, call_llama, state) is shared.

- **`config.py` webhook fields** — `WEBHOOK_MODE`,
  `WEBHOOK_HOST`, `WEBHOOK_PORT` (default 8443),
  `WEBHOOK_PATH` (default `/tg-webhook`),
  `WEBHOOK_SECRET_TOKEN`. All read from env, no defaults
  that would silently misconfigure production.

- **Operator-facing docs** — `docs/WEBHOOK.md` (initially
  ~15 KB, generic public/industry-standard form after
  v0.9.0.1) covers the full deploy: nginx server block,
  `setWebhook()` call, secret-token generation, log
  diagnostics for the common failure modes
  (Connection timed out / 502 Bad Gateway / 403 forbidden).

- **`requirements.txt`** — `aiohttp==3.9.5` for the webhook
  server. The polling loop already used `httpx`; aiohttp
  was added rather than refactoring the existing httpx
  client.

- **Webhook boot selftest** — new `_selftest` group
  exercises `make_app()` via `aiohttp.test_utils`:
  spins up a `TestServer`, hits `/healthz` and a POST with
  right + wrong secret, asserts 200/403/400 correctly.
  Webhook regression is now caught at boot.

### Changed

- **`dispatch.py` `_dispatch_update(upd_dict)`** — was the
  inner function called by the polling loop. Now also
  called by the webhook handler. No signature change;
  it already took a dict, not an `Update` object, so
  webhooks and polling share the same dispatch path.

- **`docker-compose.yml`** — bot moved from `network_mode:
  host` to `lan_macvlan` (static IP 192.168.10.5) so
  outgoing Telegram API calls get a stable source IP for
  PBR / stateful inspection; nginx in front terminates
  TLS, bot speaks plain HTTP on :8443. `WEBHOOK_MODE=1`
  and `WEBHOOK_SECRET_TOKEN=...` added to the env.

- **Production deploy packaging** — `/opt/telegram-bot/`
  on llmhost2 is the single source of truth; the worktree
  at `/opt/llama.cpp_telegram/` is rebuilt into a Docker
  image and `docker compose up -d` deploys. Two-directory
  layout (worktree ≠ deploy) is documented in
  `OPERATOR-TESTS-MORNING.md`.

### Fixed (hotfix chain v0.7.8 → v0.8.2)

This release was 11 hotfixes deep before it shipped.
Each was a regression caught by the in-tree `_selftest`
or by operator UI testing; the chain is the audit trail.

- **v0.7.8** — `_check_user_slot` rejected the third
  concurrent message with "Бот уже обрабатывает 2 твоих
  запросов". Replaced the `if in_flight >= MAX: reject`
  with `asyncio.wait_for(sem.acquire(), timeout=300)`. The
  bot runs in an asyncio task, so the wait doesn't block
  the polling/webhook loop; messages just queue per-user
  and `💭 думаю…` updates keep coming.

- **v0.7.9** — Telegram's flood-control rejected bot sends
  with `RetryAfter` after 7+ messages in a burst. `_reply`
  now catches `RetryAfter` and retries up to 3 times with
  `sleep(retry_after + 1)`.

- **v0.7.9b** — `cmd_callback`'s `edit_message_text("⏹ Уже завершено")`
  also hits the same flood-control path. Added a sliding-
  window `_rate_limit_send(chat_id)` (60s × 15 sends) and
  made the edit retry the same way.

- **v0.8.0** — `handle_photo`, `handle_voice`,
  `handle_document` all had a race: if the LLM call
  returned microseconds after the user clicked Stop, the
  bot would still send the response. Each handler now
  checks `abort_event.is_set()` after `call_llama()`
  returns and discards the response even if non-`None`.
  `handle_voice` was also missing an `if bot_response is
  None` check; that branch crashed before the abort check
  could run. New regression test in `_selftest` exercises
  the post-race discard path.

- **v0.8.1** — llama-server returned `HTTP 500` on some
  vision requests when the PEG grammar parser rejected
  malformed tool-call JSON. `call_llama` now retries
  once without tools offered: without tools in the
  request, the model can't produce tool calls, so the
  grammar has nothing to reject. User-visible: vision
  requests that previously 500'd now transparently
  succeed.

- **v0.8.2** — CodeQL alert `actions/missing-workflow-
  permissions` (CWE-275, Medium) on `.github/workflows/
  lint.yml`. Added explicit `permissions: contents: read`
  block. `GITHUB_TOKEN` is now downscoped to read-only;
  no write operations are possible with it.

### Fixed (production hotfixes v0.7.4 → v0.7.7)

These landed before the v0.7.8 chain but were bundled
into v0.9.0 for release coherence.

- **v0.7.4** — added `_stop_button_markup` helper; the
  Stop inline button (callback_data="stop:<chat_id>:
  <user_id>:<update_id>") is now sticky: even after
  `Stop` is clicked and the message is deleted, the
  user sees the next LLM reply with the same button
  attached until the bot's own LLM is fully cancelled.

- **v0.7.5** — `handle_voice` was missing the Stop
  button markup entirely. The voice handler was
  emitting `final_text` but not wrapping it in
  `ReplyKeyboardMarkup` for the reply. Fixed by going
  through the same `_reply` helper that the other
  three handlers use.

- **v0.7.6** — Stop button clicked but the LLM call
  continued. Root cause: `httpx.AsyncClient.post()` is
  blocking — by the time `call_llama()` returned, the
  LLM had already finished. New `_abortable_post`
  helper races `client.post()` against
  `abort_event.wait()` via `asyncio.wait(...)` with
  `return_when=FIRST_COMPLETED`. If abort wins, the
  in-flight POST is cancelled and `None` is returned.

- **v0.7.7** — `handle_photo` crashed with `TypeError:
  object of type 'NoneType' has no len()` on abort.
  The handler did `print(f"...{len(content)}...")`
  for logging, but `content` was `None` because
  `_abortable_post` returned `None` on abort. Guarded
  with `if content else 0`.

### Known limitations

- **macvlan isolation** — the bot container on
  `lan_macvlan` cannot reach the parent host's
  `192.168.10.7` (kernel-level isolation: macvlan
  sub-interfaces cannot ARP their parent). `WHISPER_URL
  =http://192.168.10.7:8000` is therefore unreachable;
  voice messages don't transcribe. Workaround: move
  whisper to its own macvlan with a free IP (`.5`, `.8`),
  or onto the llama-server host (`.6`).

- **`ip_address` in `getWebhookInfo` shows `85.136.112.144`,
  not the bot's LAN source** — bot's outgoing NAT goes
  through the gw1 egress path. Cosmetic; doesn't affect
  webhook delivery.

- **Telegram-specific scrubbing risk** — gw1's hosting
  provider may run JA3-based anti-bot on the network
  edge. The bot is reachable from the bot host and
  from regular curl/Chrome traffic, but Telegram's Go
  net/http TLS fingerprint is distinct. If `last_error
  =Connection timed out` reappears and `nginx -T` shows
  the right server block, the immediate workaround is
  Cloudflare Tunnel in front of the bot, which terminates
  TLS on Cloudflare's edge and re-originates over a
  long-lived outbound connection from `cloudflared` to
  the bot host. CF egress IP is universally whitelisted
  by Russian/EU hosters; the bot's source IP is no longer
  exposed to the public internet.

## [v0.9.1] — 2026-10-11 — T25 hotfix: image abort rollback, whisper on macvlan

Two operator-caught regressions from the v0.9.0 rollout,
fixed at the end of the gw1 deployment cycle. The first
was a real code bug (image persisted to history before
abort_event was registered); the second was an infra
miss (whisper still on the parent host, unreachable
from the macvlan bot container).

### Fixed (v0.9.1, hotfix chain)

- **Image abort leaves orphan in history** (operator
  report, 2026-10-11 ~21:50 MSK). On `handle_photo`,
  `await _persist_message(...)` was called *before*
  `thinking = await _reply(...)` and `_register_abort_event`.
  If the user clicked Stop after the image was already
  saved to history but before the LLM call returned, the
  image stayed in history. The next text-only message
  was answered about the (cancelled) image because the
  LLM still had the photo in its context. **Fix in
  \`handlers.py:handle_photo\`**: reordered so the thinking
  message is sent and the abort_event is registered
  *first*; `_persist_message` only runs after both are
  in place. Added two rollback paths: in the `except`
  block (any exception after persist) and in the abort
  branch (Stop button after persist). Both call the
  new `Storage.delete_last_user_message(chat_id,
  thread_id)` (added in `storage.py`) which deletes the
  most recent `user`-role row in a thread. **Regression
  test**: post a bad `file_id` via the webhook, then
  send a follow-up text — the bot's response is no
  longer about the (phantom) image.

- **Voice (whisper) unreachable from macvlan bot**.
  whisper-api was running with `network_mode: host` on
  192.168.10.7:8000, but the bot is in `lan_macvlan`
  with static IP 192.168.10.5. macvlan sub-interfaces
  cannot ARP their parent host (well-known kernel
  isolation), so `socket.create_connection((192.168.10.7,
  8000))` from the bot timed out. **Fix**: moved
  whisper-api to `lan_macvlan` with static IP
  192.168.10.3 (the IP previously used by the dev
  nginx container, now decommissioned). Updated
  `WHISPER_URL=http://192.168.10.3:8000` in the bot's
  `docker-compose.yml`. whisper's model cache volume
  (`./cache`) was preserved across the move, so no
  1.5 GB re-download.

### Known limitations (unchanged from v0.9.0)

- macvlan bot cannot reach 192.168.10.7 (parent host)
  for any non-macvlan service. The remaining
  parent-host service is the bot's own `data/` and
  `files/` bind mounts, which work because they're
  filesystem-level not network-level.

## [v0.9.2] — 2026-10-11 — review hotfix chain: handlers rollback + webhook hardening

Pre-release review (REVIEW-MINIMAX-v7) caught 4 P0 issues
that block a clean public release. v0.9.2 closes all of them
plus 2 P1 issues and adds 7 unit tests.

### Fixed (v0.9.2)

- **handle_voice + handle_document had the same bug v0.9.1
  fixed in handle_photo** (P0-1). Persist happened BEFORE the
  thinking message and abort_event registration; a Stop click
  during the LLM call left the transcript / document content
  in history, so the next user message was answered about the
  (cancelled) voice or document. Applied the same
  reordering + `Storage.delete_last_user_message` rollback
  in both abort and except branches. handle_text also got the
  same fix for consistency. `handlers.py` total change:
  ~120 lines across the three handlers.

- **webhook.py used `__import__("json")` instead of
  top-level `import json`** (P0-2). The pattern was a copy-
  paste artefact with no circular-import justification (json
  is stdlib). Replaced with a normal `import json` at the top
  of the module. One-line behavioural change, no logic diff.

- **webhook.py had no body size limit** (P0-3). A
  `request.read()` with no cap lets a malicious actor who
  learns the secret token (or finds a path that bypasses
  the check) OOM the bot. Added a 1 MB cap both via
  `web.Application(client_max_size=1*1024*1024)` and an
  explicit re-check in `_handle()`. Telegram updates are
  < 100 KB; 1 MB is 10x the legitimate max.

- **webhook.py fire-and-forget task silently swallowed
  exceptions** (P1-1). If `_dispatch_update` raised, the
  loop's default handler logged `Task exception was never
  retrieved` to stderr but the operator never saw it because
  the webhook had already returned 200 OK. Wrapped the
  dispatch task body in a try/except that calls
  `log.exception(...)`. The webhook still returns 200
  (the request itself was valid); the traceback is now
  in `docker logs`.

- **config.py referenced a non-existent `WEBHOOK_URL`
  constant** (P1-2). The comment said "WEBHOOK_URL is the
  public URL Telegram should POST to" but no such constant
  existed. Defined it: `WEBHOOK_URL = os.environ.get(...)`.
  In `dispatch.py:main()`, if `WEBHOOK_URL` is set AND
  `WEBHOOK_SECRET_TOKEN` is set, the bot auto-registers
  the webhook with Telegram on startup via
  `bot.setWebhook(url=..., secret_token=...)`. If
  `WEBHOOK_URL` is empty, register manually (see
  docs/WEBHOOK.md section 4.5).

- **call_llama.py: `_abortable_post` caught
  `asyncio.CancelledError` in a combined except clause
  with `Exception`**. Split into two except blocks: bare
  `asyncio.CancelledError: pass` and `Exception: pass`.
  The combined clause was correct in practice but
  unnecessarily broad; the split makes the cancellation
  intent explicit and matches the canonical asyncio pattern.

- **handlers.py: handle_text comment "handle_photo uses
  is_group in print below" was misleading** — handle_text
  does not use `is_group` in any print. Replaced with
  "is_group is unused in this handler (tuple shape kept
  for symmetry)".

- **handlers.py: handle_voice had no `persisted` flag** —
  now added; handle_document and handle_text also have it.

### Test coverage added (v0.9.2)

7 new pytest tests in `tests/test_selftest.py`:

- `test_delete_last_user_message_removes_persisted_image` —
  writes a user message, calls `delete_last_user_message`,
  asserts the row is gone.
- `test_delete_last_user_message_returns_false_when_empty` —
  empty thread, asserts return value is `False`.
- `test_delete_last_user_message_only_removes_user_role` —
  writes user + bot + tool rows, asserts only the user
  row is removed (regression test for the bot/tool rows
  being accidentally deleted on rollback).
- `test_webhook_check_secret_accepts_valid_token` —
  `monkeypatch` sets `WEBHOOK_SECRET_TOKEN`, request
  has matching header, asserts `_check_secret` returns True.
- `test_webhook_check_secret_rejects_invalid_token` —
  header has different value, asserts False.
- `test_webhook_check_secret_rejects_missing_token` —
  no header, asserts False.
- `test_webhook_check_secret_rejects_when_unconfigured` —
  `WEBHOOK_SECRET_TOKEN` empty, asserts False for any
  header value (safe-by-default behaviour).

All 12 tests pass in 0.22s.

### Documentation updates (v0.9.2)

- `docs/WEBHOOK.md` — `127.0.0.1` claim replaced with
  `WEBHOOK_HOST:WEBHOOK_PORT` and a note that the default
  is `0.0.0.0` for macvlan production. Added new section 4.3.1
  covering the `docker network create -d macvlan` setup.
  Section 4.4 updated to include `WEBHOOK_URL` and document
  auto-registration. Section 4.5 now points to the
  auto-register path first and the manual curl as the
  fallback.
- `CHANGELOG.md` — this section.

### Operator action required

None for the public repo. The previous v0.9.1 (commit
`20b5cd9`) already moved secrets to `.env` and rotated the
Telegram bot token. v0.9.2 adds no new env vars that must
be set; `WEBHOOK_URL` is optional. If you want auto-
register on startup, set:

```
WEBHOOK_URL=https://<your-subdomain>.<your-domain>/tg-webhook
```

in `telegram-bot/.env` and restart.
## [v0.9.3] — 2026-10-11 — auto-register footgun fixes + cleanup

Post-v0.9.2 review (REVIEW-MINIMAX-v8) caught 2 NEW P0 bugs in
the v0.9.2 auto-register feature. v0.9.3 closes them plus 2
cleanup items. None of the bugs were live in production
(WEBHOOK_URL was empty; the operator registered manually),
but they would have manifested the moment auto-register was
enabled.

### Fixed (v0.9.3)

- **Auto-register missing `allowed_updates`** (P0-NEW-1).
  The v0.9.2 set_webhook call did not pass `allowed_updates`,
  so Telegram defaulted to `["message", "edited_message"]`
  only — silently dropping `callback_query` updates (the
  Stop button). Added `allowed_updates=["message",
  "edited_message", "callback_query", "chat_member"]` to the
  registration call. Without `callback_query`, the Stop
  button appears in the UI but clicks are lost; the LLM
  runs to completion ignoring the user intent.

- **Auto-register used double `asyncio.run()` loop** (P0-NEW-2).
  The v0.9.2 code did `asyncio.run(_auto_register())` followed
  by `asyncio.run(_webhook_run())`. The first loop initialized
  a Bot object whose httpx client was closed when the loop
  exited. The second loop started the webhook server, but
  every `context.bot.send_message(...)` call failed because
  the Bot's httpx client was dead. Replaced with a synchronous
  HTTP call (`urllib`) for the one-shot `set_webhook`
  registration. The Bot is now initialized lazily inside
  the webhook event loop, where its httpx client lives for
  the duration of the process. New helper:
  `dispatch._webhook_auto_register_sync(url, secret_token)`.

- **WEBHOOK.md section 4.5 manual-register curl lost
  `allowed_updates`** (P2 paired with P0-NEW-1). The v0.9.2
  doc had a simpler curl that did not include
  `allowed_updates`. Updated to use the JSON-body form with
  `allowed_updates=["message", "edited_message",
  "callback_query", "chat_member"]` and added a callout
  paragraph explaining why `allowed_updates` is mandatory.

- **tests/test_selftest.py imports in middle of file**
  (P3). v0.9.2 had `import os / json / asyncio / hmac /
  MagicMock / pytest` placed AFTER
  `test_rating_parser_returns_body`. Moved `MagicMock` to
  the top imports; removed duplicate `os`/`asyncio`/`pytest`;
  removed unused `json`/`hmac`.

### Documentation added

- `docs/REVIEW-MINIMAX-v8.md` — the post-v0.9.2 review wave
  that surfaced the 2 new P0s. 8 KB.

### Operator action required

None. The 2 P0s were dormant in production (WEBHOOK_URL was
empty). If you intend to use auto-register in the future, the
v0.9.3 fix is now in place. Just set `WEBHOOK_URL` in
`telegram-bot/.env` and restart.
## [v0.9.4] — 2026-10-11 — idempotent auto-register + JSON body + comprehensive tests

Final pre-tests review (REVIEW-MINIMAX-v9) caught 1 P1
deployment-hygiene issue (silent webhook overwrite) and
2 P3 issues (string `drop_pending_updates`, response body
not parsed). v0.9.4 closes them and adds the first
**comprehensive unit-test suite for the entire pipeline**
(text, voice, image, document handlers, webhook auth + body
cap, auto-register, storage, rollback patterns) so the
operator can stop hand-testing every release.

### Fixed (v0.9.4)

- **Auto-register silently overwrites working webhook**
  (P1-NEW-1). The v0.9.3 helper blindly called `setWebhook`
  with whatever `WEBHOOK_URL` was in the env, so a typo or
  staging URL in env would overwrite a working production
  webhook. The v0.9.4 helper first calls `getWebhookInfo`
  to see the current state. If the current URL matches
  `WEBHOOK_URL`, it skips `setWebhook` (idempotent path
  prints "webhook already registered"). If different, it
  prints a 3-line WARNING before overwriting.

- **`drop_pending_updates` was sent as string "false"**
  (P3-NEW-1). Telegram's Bot API spec says it should be a
  Boolean. Switched the helper to use a JSON body
  (`Content-Type: application/json`) so `drop_pending_updates`
  is a real `False` and `allowed_updates` is a real JSON
  list, not a stringified one.

- **Auto-register response body not parsed for error details**
  (P3-NEW-2). If Telegram returns `{"ok": false,
  "description": "Bad webhook: ..."}`, the v0.9.3 helper
  printed the HTTP error but not the actual Telegram
  description. v0.9.4 parses the response JSON; if
  `ok` is false, prints `description`. Distinguishes
  HTTPError (network/proxy) from Telegram-level errors.

### Tests added (v0.9.4)

This is the comprehensive test suite. New file
`tests/test_handlers.py` covers the rollback pattern in
all 4 handlers (text/voice/photo/document) for both the
abort and the exception paths. New file `tests/test_webhook.py`
covers webhook auth (already in test_selftest), body cap
(NEW), safe_dispatch exception logging (NEW), and
`_webhook_auto_register_sync` (NEW: 4 tests covering payload
correctness, idempotent skip, overwrite warning, JSON
body format). New file `tests/test_storage.py` covers
storage operations including the v0.9.1+ delete_last_user_message
edge cases.

Total pytest count: 12 -> 32+ tests, all in 0.3-0.4s.

### Documentation added

- `docs/REVIEW-MINIMAX-v9.md` - the final pre-tests review
  wave that surfaced the P1 + 2 P3 issues.

### Operator action required

None. The fixes are additive. The comprehensive test suite
lets the operator skip the manual smoke test for these
scenarios on future releases.
## [v0.9.5] — 2026-10-12 — test suite polish + coverage expansion

v0.9.4 review (REVIEW-MINIMAX-v10, wave 12) caught 7 P3 items
in the new test suite. v0.9.5 closes 6 of them. T1 (DRY up
handler test setup) is deferred — the fixture approach had
subtle ordering issues that need a deeper investigation, and
the inlined setup is small enough (12 lines × 7 tests) to
be acceptable for now.

### Fixed (v0.9.5)

- **Dead `_pers._store_instance` line in 7 handler tests** (T2).
  The line was a no-op because `persistence.py` doesn't have
  `_store_instance`; it was an artefact of a previous pattern.
  Removed all 7 instances.

- **conftest.py comment said "35+" tests, actual is 36** (T6).
  One-word fix.

### Tests added (v0.9.5)

- `test_auto_register_handles_empty_current_url` (T3) — first-deploy
  case. getWebhookInfo returns `{"result": {"url": ""}}` and the
  helper proceeds with setWebhook without printing a WARNING
  (because the existing webhook is empty, not different).

- 3 tests for `call_llama._abortable_post` (T4):
  - `test_abortable_post_returns_response_when_post_finishes_first`
  - `test_abortable_post_returns_none_when_abort_fires_first` —
    exercises the v0.7.6 race condition fix.
  - `test_abortable_post_passes_through_when_no_abort_event`

- 3 tests for `storage` known_topics (T5):
  - `test_known_topics_round_trip` — add / find by name / find by id
    / remove
  - `test_known_topics_list_per_chat` — list_known_topics returns
    only topics for the given chat
  - `test_known_topics_returns_none_for_missing`

Total pytest count: 36 -> 43 tests, all in 0.48s.

### Documentation added

- `docs/REVIEW-MINIMAX-v10.md` (T7) — the v0.9.4 test suite
  review wave that surfaced the 7 P3 items.

### Deferred to a future release

- **T1: DRY up test_handlers.py setup** (P2, 30 min).
  The 12-line DB setup block is repeated 7 times. The reviewer
  suggested a pytest fixture (`fresh_store`) but the fixture
  approach caused test isolation issues (the fixture's
  monkeypatch+Storage setup interfered with the test's inlined
  setup). Will revisit with a context-manager approach
  (`with fresh_db() as store:`) in a follow-up.
## [v0.8.0] — 2026-10-10 — v0.7.8 → v0.7.9b + v0.8.0 → v0.8.2 chain

Hotfix chain documented in v0.9.0 above. This entry
exists so the chain has a single-versioned anchor for
`git tag` purposes; the actual changes are in v0.9.0.

## [v0.7.0] — 2026-10-09 — production hardening baseline

First production-grade run. Code that survived three
operator-test waves plus one set of in-tree `_selftest`
regressions. Predecessor of v0.9.0 (F3 + webhook + hotfix
chain).

### Added

- **5 pytest tests** in `tests/test_selftest.py` covering
  `_selftest()` runs end-to-end, the store singleton
  invariant, the constants module, the F4 LRU bound
  (200 entries max), and the rating parser's body-
  extraction path.

- **19 in-tree selftest groups** in `bot.py:_selftest()`,
  all 19/19 PASS in Docker at this version. Documented
  the selftest bootstrap sequence in `OPERATOR-TESTS-
  MORNING.md`.

- **CodeQL workflow** at `.github/workflows/lint.yml`
  running `ruff` + `mypy` on every push and PR.


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
