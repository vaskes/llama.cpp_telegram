# docs/TODO.md — the active backlog

> This is the **only** live backlog in the repo. Everything else that looks
> like a task list is one of:
>
> - `CHANGELOG.md` — what shipped, and when;
> - `TECH-DEBT.md` + `docs/tech-debt/` — what was measured, deliberately
>   left alone, and what would change that answer;
> - `docs/tickets/` — the context-memory block (T65–T70), shipped, with
>   the open questions recorded;
> - `docs/archive/` — dated records, kept for the "how did we get here",
>   not for instructions.
>
> **Updated 2026-10-23**, against the `v0.13.0-stable` tag.
>
> The previous version of this file was last touched 2026-10-13 and had
> accumulated four kinds of rot: a status snapshot from before the
> context-memory rewrite (it said the bot ran v0.9.1), a checklist for an
> operator-test file that no longer exists, a "code freeze" from a
> pre-`v0.10.2` cycle, and eleven ticket entries marked open that had been
> closed for months. It was the same failure the `TECH-DEBT.md` index had:
> a second copy of the truth, maintained by hand, drifting.

---

## State of the tree (measured, not remembered)

| | |
|---|---|
| HEAD | twenty-two commits past the `v0.13.0-stable` tag (trailer cleanup, codebook, the `dev` limits profile, the Stop-button gate, the answer-files feature, the offline replay of it, the chunked summaries, the removal of edit-replace) |
| Production | **the host runs on a generator** — any request to the model overloads it (operator's measurement, 2026-10-24), so neither the bot nor the engine can be exercised there for now. The box also lost power earlier the same day |
| Pytest | `2215 passed, 20 skipped` locally |
| CI | `lint.yml`: ruff (advisory) + pytest on Python **3.11** and **3.12** — `2220 passed, 1 skipped` on both at `1b46b79` |
| Production interpreter | Python **3.11** (`telegram-bot/Dockerfile` pins `python:3.11-slim`) |
| Production modules | 54 `.py` files in `telegram-bot/` |
| In-tree selftest | `selftest.py` (239 lines) + 7 leaf modules, `_selftest` = 111 lines |
| Open review cycle | none — waves 1–8 closed just before the `v0.13.0-stable` tag ("production-ready") |

Sizes are `splitlines()`-based and are recomputed by
`tests/test_tech_debt_index.py`, which fails if the numbers in
`TECH-DEBT.md` go stale.

---

## Open — waiting on a decision from the operator

These are not forgotten; they are parked on a question only the operator
can answer. Each one records the decision that unblocks it.

### The answer-files feature has never run in production — the generator takes the model load

**Status:** open since 2026-10-24. The feature is implemented, tested and
pushed (`e8e4ff0`); what is missing is the live check.

**What the operator measured:** the production host runs on a generator, and
any request to the model overloads it. So the bot cannot be exercised
against the real engine for now — not because the code is wrong, but
because the box cannot take the load. (Earlier the same day the host also
had no power at all.)

**What was done instead:** `tests/test_artifact_e2e.py` — the real handler,
the real parser, the real sender and the real database, with the engine
replaced by the answer shapes that were measured on the live engine on
2026-10-24. It runs in CI, and as a script it prints the transcript of
each shape:

    python tests/test_artifact_e2e.py

**What is still missing, and what it needs:** the live check itself, once
the generator can take the load. The checklist:

1. deploy the current `main` to the production bot;
2. ask the model for a json, an html page, a site in three files, and one
   plain question with no code at all;
3. check that the code arrives as a **document** with the right name and
   extension, and that the chat gets the prose and not the code;
4. ask a follow-up about the file ("поменяй в нём цвет") and check that
   the bot still remembers the code — that is the whole point of keeping
   the raw answer in the history;
5. check the topic: a document sent from a forum topic has to land in
   that topic, not in General;
6. check that the reasoning-only answer still arrives as a diagnostic and
   not as a document.

**Decision needed:** when the host can take the model load, and who runs
the checklist.

### The context blocker is not closed — and its list is growing

**Status:** open. The operator restated this after the 2026-10-23 test
session: T65–T70 shipped and are recorded as closed, but the underlying
blocker -- *how the bot keeps a coherent context over a long
conversation* -- was covered by that work, not solved by it. The pieces
below are all parts of it, and the list is growing rather than shrinking:

| Piece | What it still leaves open |
|---|---|
| T68b | the engine keeps a KV cache for context that already fell out of the window, and the reset command is unreachable |

Closing the context-memory tickets closed *a* set of tasks. It did not
close this. Treat the row above as the blocker, and do not read a green
board here as "the context problem is done".
(T70.6 left this table on 2026-10-23: the operator's decision was that
the thinking cap belongs to llama-server, so the bot-side knob was
deleted rather than implemented -- see that entry.
T70.5 left this table on 2026-10-24: the operator decided on chunked
summaries with a "СВЕДЕНО" marker in the database, and it is
implemented -- see that entry.
T66.1 left this table on 2026-10-24: the operator's answer was recorded
on 2026-10-20 -- "not adding" -- and it was never a piece of this
blocker. See the closed list.)


### T68b — `BOT_ENGINE_RESET_CMD` is unreachable in every mode

**Status:** deferred by the operator 2026-10-22, as part of a related
group rather than as a standalone defect.
**Where:** `docs/adr/ADR-0003-context-reanchoring.md`.

Two mutually exclusive gates. `start_reanchor_scheduler()` returns
immediately when `SOFT_REANCHOR_ENABLED` is set — that is, whenever the
mode is not `soft` — so in `process+engine` there is no in-process
scheduler at all. And inside the loop the engine reset only runs when
`REANCHOR_MODE == "process+engine"`. The two sets do not intersect.

The command is needed exactly in `process+engine`, which is the default:
the bot restarts the operator on a schedule, and the engine keeps holding
a KV cache for context that has already fallen out of the window. Nobody
can execute it.

**In production `BOT_ENGINE_RESET_CMD` is not set**, so the wiring is dead
but harmless: there is nothing to execute.

Two options, both waiting on an operational decision:

- **(a)** a second in-process task that sleeps until the same boundary
  (`BOT_REANCHOR_AT`) and calls `run_engine_reset()` in `process+engine`.
  The boundary arithmetic already exists and is DST-correct
  (`seconds_until_next`); it was written for the other loop.
- **(b)** move the command into the operator's cron/systemd unit and drop
  `BOT_ENGINE_RESET_CMD` from `.env.example` and ADR-0003.

(a) is technically cleaner — the bot already owns the boundary computation
and the setting is advertised as the bot's own. But it is an operational
decision: the bot would restart `llama-server` once a day and stop
answering for the duration. **Implementation of (a) needs the operator's
explicit go-ahead.**

### RETHINK — the wording of `--reasoning-budget-message`

**Status:** open, **assigned to the operator** (vaskes), 2026-10-23.
**Where:** the engine unit on the host,
`/etc/systemd/system/llama-qwen5090-256k.service`. The repo's
`systemd/` holds only the compose units, so this file is host-only and
is not versioned here.

Added to the unit, with a `RETHINK` marker in the comment above the flag:

    --reasoning-budget-message "Reasoning budget exhausted. Stop thinking and give the user the final answer now."

**What it does, measured** on the build this box runs (llama.cpp
0.4.1-dev, commit b49650a). The reasoning-budget sampler counts the
tokens inside the thinking block; when the budget runs out it forces a
token sequence, and the sequence is the message followed by the first
end-of-thinking tag (`tools/server/server-schema.cpp`: the message is
*prepended* to the tag, so the tag must NOT be written into the message
yourself). Measured here with a per-request budget of 64 tokens:

| | what the model did after the cut |
|---|---|
| without the message | continued the interrupted sentence (`17 × 23 = 17 × (20 + 3`) and only then answered |
| with the message | wrote the final answer immediately (`**17 × 23 = 391**` plus the full calculation) |

Both the budget and the message can be set per request
(`thinking_budget_tokens`, `reasoning_budget_message` in the request
body), and the per-request budget overrides the CLI one -- so the
behaviour was verified **without restarting the engine**.

**Why it is flagged RETHINK and not "done":** the operator asked for the
flag to be added and the call handed back to them, to be revisited after
the current test run. Two things are still open:

1. the wording is English, and it is forced *inside* the model's own
   reasoning -- it is an instruction to the model, not a user-visible
   message, and it will show up in the reasoning trace if the model
   echoes it;
2. it only fires when the 64K thinking budget is actually exhausted. The
   2026-10-23 Newton's-cradle run reached ~57-58K reasoning tokens and
   then moved to the tool loop on its own, so the cut never fired there.

**What it needs:** the operator's call after the test run -- keep the
wording, change it, or drop the flag again.


## Open — code, not blocked on anyone

### T-FREEZE-EMPTY-1 — the empty-response fallback never reaches the user

**Status:** open since 2026-10-24. Found by the offline replay of the
answer-files feature, not by a test that was written for it.

When the model spends its whole budget on reasoning, `call_llama` returns
a diagnostic string that starts with `EMPTY_RESPONSE_PREFIX`. The handler
retries once; if the retry is empty too, it assigns
`EMPTY_RESPONSE_USER_FALLBACK` to `bot_response` — and then
`_persist_for_reply` returns the answer's prose over it. The fallback is
discarded, and the user gets the diagnostic with the reasoning tail
instead of the friendly "не удалось получить ответ" message.

**Measured on the production bot, 2026-10-24:** assistant turn id 10369 in
the production database, 3618 characters — the diagnostic, with a
45108-character reasoning tail behind it.

**Why it was not fixed inside the answer-files change:** it is a different
feature (the empty-answer retry, 2026-10-22), and the operator has to
choose what the user should see — the friendly fallback or the raw
diagnostic. Changing it would change production behaviour under cover of
another feature, which is exactly what the CODEBOOK's "one concern" rule
exists to prevent.

**Note:** `tests/test_artifact_e2e.py` pins the current behaviour (the
diagnostic reaches the chat), so fixing this will break that test on
purpose.

**What it needs:** the operator's choice, and then the fix in
`_persist_for_reply` or in the handlers, plus the test that the user
actually sees the chosen text.


### T-FREEZE-AUTO-3 — a deploy workflow (`.github/workflows/deploy.yml`)

**Status:** open, **needs an operator decision and a secret.**

CI/CD that deploys on tag push. It needs a deploy SSH key in the
repository's Actions secrets, and the deployment topology (compose file
path, `.env` handling, volume mounts) is the operator's, not the repo's.
There is no secret configured for it today.

---

## Accepted as documented risk — not a bug, not a task

### T-FREEZE-1 — the semaphore LRU can evict a held semaphore

`_user_semaphores` is an LRU capped at `_USER_SEMAPHORES_MAXSIZE`, and the
eviction does not check whether the semaphore it drops is currently held.
The next message from that user therefore gets a *fresh* semaphore with
the full `_PER_USER_SEMAPHORE_LIMIT` budget, so the per-user concurrency
cap can be exceeded.

Accepted for the current deployment: the whitelist is short, so the dict
holds a handful of entries and never approaches the 500-entry cap
(`_USER_SEMAPHORES_MAXSIZE`). Revisit if the deployment scales to many
users or if LOCKDOWN is relaxed.

`state._get_user_semaphore` is the function; the cap is
`_PER_USER_SEMAPHORE_LIMIT` in the same module.

### T-FREEZE-STOP-1 — the Stop button "does not always work"

**Not reproducible.** The operator pressed Stop 100 times after the
deploys stopped, and every press stopped the turn.

What the surviving log shows: the container was recreated on every
deploy, so only the last instance still has its stdout. It holds four
Stop presses -- four `abort_event set`, four `ABORTED by user via Stop
button`, four rolled-back persisted turns, zero failures, zero
rejections.

The earlier misses cannot be diagnosed: the containers that saw them
were removed and their logs went with them. Two paths in
`telegram-bot/commands/callback.py` can produce that symptom with the
button itself blameless -- the press arrived while the webhook was
down (Telegram gets a 502 on the POST and drops the update, so the
callback never reaches the bot), or the turn had already finished (the
handler answers "already done" and logs `no abort_event for ...`).

The rule stays maker-taker: only the author of the message may stop
its processing, and the check is `clicker_id != original_user_id`
(`telegram-bot/commands/callback.py`).

### T-FREEZE-2 — `call_llama` was a god function

**Closed, differently.** It was not "observed in production long enough":
it was measured, split, and measured again. `call_llama.py` is now 478
lines with 17 `llm_*` leaf modules, and the tool loop is `ToolLoop.run_one`
/ `ToolLoop.run`. See `TECH-DEBT.md` items 3 and 8.

### T-FREEZE-3 — `BOT_COMMANDS` mixes Russian and English

Cosmetic and intentional: Russian descriptions with English technical terms
("sub-talk"). Left as-is.

### T-FREEZE-4 — `_RATING_PREFIX_RE` / `_EMOJI_CHAR_RE` were unified

False alarm — the Qwen report was wrong. Already correct.

### T-FREEZE-MT-1..23 — the operator's manual checklist

Executed against `v0.10.2` by the operator and a second tester (Vaskes and
Dimon, 2026-10-12). The only item that was deliberately not done is the
live edit-replace scenario — and the feature it covered was removed on
2026-10-24, so the scenario is no longer part of the checklist.

---

## Closed in the current cycle — where to read it

Not repeated here; each line points at the record.

| Work | Record |
|---|---|
| Context memory T65–T70 | `CHANGELOG.md` § v0.13.0-stable; `docs/tickets/README.md` |
| `dispatch.py` 1612 → 345 | `TECH-DEBT.md` #10; `docs/tech-debt/10-*.md` |
| `call_llama` 1616 → 478 | `TECH-DEBT.md` #3, #8 |
| `_selftest` 1940 → 111 | `TECH-DEBT.md` #9 |
| `bot.py` → 124-line facade | `TECH-DEBT.md` #1 |
| `handlers.py` shared wrapper | `TECH-DEBT.md` #13 |
| `prompts.py` prose → `prompt_text/` | `TECH-DEBT.md` #12 |
| Env-var name validation | `TECH-DEBT.md` #11 |
| The edit-replace feature, removed | `CHANGELOG.md` § Unreleased; `docs/tech-debt/10-*.md` |
| T66.1 — the guaranteed minimum, decided | `docs/tickets/README.md` (open question 3); `docs/tickets/TICKET-T66-*.md` |
| T70.5 — chunked summaries with a "СВЕДЕНО" marker | `CHANGELOG.md` § Unreleased; `docs/tickets/TICKET-T70-*.md` |
| T70.6 — the reasoning cap lives on the engine | `CHANGELOG.md` § Unreleased; `docs/CALL_LLAMA.md` |
| Review waves 1–8 | `docs/archive/2026-10/`; `docs/review/README.md` |

---

## Closed — and why (the reasoning behind the closed items)

The items below were decided and are done. They are here because the
*reasoning* is worth keeping, not because they are pending.

### T70.5 — what if the summary itself does not fit in the context window?

**Status:** ✅ **decided by the operator 2026-10-24, implemented.**
**Where:** `docs/tickets/TICKET-T70-context-protocol-redesign.md`,
`docs/tickets/README.md` (open question 6), `telegram-bot/summarize.py`,
`telegram-bot/context_budget.py`.

The operator's answer: chunk the old part, summarise each chunk, and mark
it in the database so the same chunk is never summarised again.

> сводки надо делать чанками, и помечать. т.е. если ОБЪЕМ данных для сводки
> > 200к надо сделав по ней сводку проставить в базе поле СВЕДЕНО, ссылку на
> сводку которая сделана и по этому чанку больше сводки не делать. Если объем
> данных для сводки <200к не проставлять поле сведено и не сводить, а грузить
> как есть в контекст. Ну и при загрузке соответсвенно грузить только готовые
> сводики и только сообщение которые не сведены.

**The 200k became 64k**, and that is the one number in this entry that is
not the operator's: 200k of raw history plus the fresh window plus llama's
64k thinking block does not fit in a 256k window, and it fails the way
T70.6 failed ("the model thought and did not answer"). 64k fits:

    262144 - 65536 (thinking) - 16384 (answer) = 180224 for the prompt
    180224 - ~48000 (fresh window) - ~4000 (system + tools)
      = ~128000 for the old part  ->  SUMMARY_CONTEXT_TOKENS = 131072

**What is in the code now:**

- `SUMMARY_TAIL_TOKENS` (64k) -- how much old, never-summarised history is
  loaded raw. Below it: nothing is summarised, nothing is marked, the old
  part goes into the context as is.
- `SUMMARY_CHUNK_TOKENS` (64k) -- how much old history goes into one
  summarisation call.
- `SUMMARY_CONTEXT_TOKENS` (128k) -- the ceiling on the old part as a
  whole; when it does not fit, the oldest goes.
- `summaries` table + `messages.summary_id`: the "СВЕДЕНО" marker with a
  link to the summary that stands for those messages. Written in one
  transaction with the summary itself.
- The context is assembled as: the summaries (each standing where its chunk
  was) + the messages that were never summarised + the fresh window.
- A failed chunk stays unmarked: its messages are loaded raw and it is
  retried at the next re-anchor.

**What is NOT done:** the strategy is still off by default
(`CONTEXT_STRATEGY=window`), because the production host is on a generator
and the summarise path has never run against a live engine. That is the
same live check as the answer-files feature.

**Bonus defect the rewrite removed:** the old code summarised only the last
400 old messages (`MAX_OLD_MESSAGES_PER_CALL`) and silently dropped the rest.
There is no such cap now -- a chunk is bounded by tokens, not by a row
count.

**Verification:** `tests/test_t70_5_chunked_summaries.py` (the volume rule,
the marker, the assembly, the budgets, the whole path) and
`tests/test_t70_summarize.py` (the model call, the failure paths, the
trigger placement) -- plus
`scripts/mutation_harness_t70_5.py`: 14 mutations, 14 caught, 0 survived.
Two mutations survived the first run and both were real holes: the
ordering of a summary against an older raw chunk was not pinned (the
partial-failure case), and one mutation was hitting an optimisation
rather than a rule. Both are fixed and the mutation now targets the
re-anchor call.

The `summarize` strategy builds a synthetic `user→assistant` summary pair
from history older than the anchor window. That summary is built from
*all* of the old history. If the summary alone exceeds the engine's
context window, something has to give, and the four candidates are not
equivalent:

- split the old part into several summaries;
- drop the oldest facts;
- bound by freshness inside the old part;
- accept the overflow and say so honestly.

**Why it is not urgent:** `T70.3` works without it. The question only
arises at a scale the deployment has not reached — the current history
fits in the window, so nothing is summarised into a corner.

**What unblocks it:** the operator picks one of the four, or states a
fifth. Until then the summarise path stays opt-in and off by default
(`CONTEXT_STRATEGY=window`).

### T70.6 — the reasoning limit is declared and nobody reads it

**Status:** CLOSED 2026-10-23 — the operator picked option (b) and named
the engine budget. The knob is gone from the bot; the limit lives on
the engine, where llama.cpp enforces it.
**Where:** `telegram-bot/limits.py` (the constant is removed),
`tests/test_limits.py` (two tripwires keep it from coming back), and the
engine unit (`llama-qwen5090-256k.service`, `--reasoning-budget 65536`).

**The operator's decision**, verbatim: "управление лимитами если
возможно в ллама должно происходить в лама", "бот по возможности не
является контекстным или лимитным ограничителем", engine budget 64K,
`MAX_TOKENS_DEFAULT` 128K. So: option (b) — the bot-side
`REASONING_RATIO_HARD_LIMIT` (and its env var) is **deleted**, not
implemented. A knob that is declared, validated, documented and enforced
nowhere is a trap, and the operator's rule is that the bot is not the
limiter.

**The second half of the problem is fixed by the numbers.** The engine's
budget has to be smaller than the bot's `max_tokens`, or the answer has
nowhere to go: the engine's 65536 equals the bot's old text cap (65536)
and exceeds the old document cap (`MAX_TOKENS_CAPTION` = 16384), which
is how a reasoning-only answer reached the user twice on 2026-10-23. The
`dev` profile now sets 131072 / 98304 / 3600s — every per-path cap
above the engine's thinking budget, with room for the answer after it.
`tests/test_limits.py` pins that invariant.

**Also fixed on the way:** the `dev` profile was a phantom. The name was
accepted by the validation and then fell through to the PUBLIC defaults,
so the operator's production ran the conservative numbers while
believing it ran the aggressive ones. It has its own table now, and a
test asserts it is not the public profile.

### T-FREEZE-CHUNK-1 — a long answer to a photo or a document

**Status:** ✅ **closed 2026-10-24.**

Telegram rejects a text message over 4096 characters. The text and voice
handlers split their answer; photo and document sent one message, so a
long answer to a photo came back as `Message_too_long` and the user saw
the API error instead of the answer.

The split moved into `_send_answer_text` (in `handlers.py`), and all four
handlers use it. The policy -- 4000 characters per piece -- lives in
`chat_utils.TELEGRAM_TEXT_CHUNK`, next to the splitter itself.

Two things were fixed on the way, and both were found by the new tests:

- the pieces are sent untrimmed. A trimmed piece loses the space between
  the last word of one piece and the first word of the next, and the user
  reads "строка 372строка 373";
- the pieces go through the same rate limiter as any other message.
  Telegram counts messages, not answers, and a 40k-character answer is
  ten messages in a row.

**Verification:** `tests/test_answer_chunking.py` -- the splitter itself,
the two paths that lacked the split (through the real handlers, with the
engine replaced), and the structural tripwire that no handler sends its
answer on its own. `scripts/mutation_harness_chunking.py`: 9 mutations, 7
caught, 2 survived-as-intended (the group-B reversals: remove the
tripwire *and* the behaviour, and the suite has to stay green).

### T-FREEZE-AUTO-1 — `scripts/smoke_test.py`

**Status:** ✅ **closed 2026-10-24.**

A real round trip against a real bot, with no mocks and no fixtures:
`scripts/smoke_test.py`.

What it checks:

- the token (`getMe`), and with it the bot's identity;
- the webhook (`getWebhookInfo`): the registered URL against the one the
  deployment expects, the last error, the pending queue, and the
  subscription (a stale `edited_message` in it is a warning: the feature
  is gone, the bot ignores edits anyway, and the subscription survives a
  restart);
- the round trip: a probe goes out, the answer comes back, and the
  answer is an answer rather than the empty-response diagnostic;
- the memory: the last assistant turns in the bot's own database, and
  whether the answer was kept whole;
- the log: the fatal lines that would not show up as a failed answer.

**What it cannot do, and says so in the script:** Telegram does not let
a bot act as a user -- a bot's own messages never come back as updates --
so the round trip needs a SECOND bot in the same chat (`--tester-token`,
`--chat`). And a bot cannot press an inline button, so the Stop button
stays a manual click; the script prints the three log lines to look for
and checks the log afterwards.

Standard library only: it runs on the operator's host, where installing
packages is not an option.

**Verification:** `tests/test_smoke_test_script.py` -- the script's own
logic, with the Telegram API replaced by a fake `urlopen`. What is
tested is the script, not the bot: the bot is the thing the script goes
and asks.

### T-FREEZE-DOC-1 — the troubleshooting guide after `v0.10.2`

**Status:** ✅ **closed 2026-10-24.** `docs/TROUBLESHOOTING.md` now
covers everything that arrived after `v0.10.2`.

What was added, and why each piece is a troubleshooting entry rather
than a feature description:

- **the context-memory block** -- the two strategies (`window`,
  `summarize`), the five knobs (`BOT_REMEMBER_DAYS`,
  `CONTEXT_MAX_MESSAGES`, `SUMMARY_TAIL_TOKENS`,
  `SUMMARY_CHUNK_TOKENS`, `SUMMARY_CONTEXT_TOKENS`), the anchor that
  does not slide, and what `/stats` prints. "The bot forgot" is the
  question this section has to answer, and the answer is usually the
  window.
- **the chunk summaries and the "СВЕДЕНО" marker** -- what a summary
  replaces, that a failed chunk stays raw and is retried, that a
  deleted summary brings its messages back, and the two SQL queries
  that show the state.
- **the daily re-anchor** -- the three modes, what each one needs, the
  four variables, and the known defect T68b: `BOT_ENGINE_RESET_CMD`
  never runs, because the loop that would call it only starts in
  `soft` mode.
- **the persona file** -- where it is, why `enabled: false` means *no*
  persona rather than a default one, and why the file is gitignored.
- **the dispatch split** -- which module owns what, so a symptom that
  used to mean "look in dispatch.py" has a home now.
- **the answers that arrive as documents** -- where the file name
  comes from, what is not a file, and why the chat and the history are
  two different strings.
- **the Stop button** -- the maker-taker rule, and what each of the
  three log lines means.

One stale claim was corrected on the way: the guide still said that an
edited command message is "normalised into a fresh message and
re-processed". The edit-replace feature was removed on 2026-10-24; an
update that carries only `edited_message` is dropped, with a log line.

### T-FREEZE-AUTO-2 — a second integration-test mechanism

**Status:** ✅ **closed 2026-10-24 — subsumed, on purpose.**

The recommendation was a `tests/test_integration.py` with an
`integration` marker. What exists now:

- `pytest.ini` registers the `tech_debt` marker and `asyncio_mode =
  auto`, so a marker-based split is available;
- the suite has grown by an order of magnitude since the
  recommendation was written (377 -> 2200+ tests);
- the tests that need a live side-car live in
  `tests/test_shell_integration.py`, and CI excludes them **by name**,
  not by a marker;
- the sandbox tests that need credentials skip themselves when the
  credentials are absent.

A second, parallel mechanism would have to be kept in step with the
first, and nothing it would catch is not caught today. Closed as
subsumed rather than done: the machinery is there if a future test
needs it.

## House rules for this file

1. **A task lives here or nowhere.** If it is not open, it is in the
   changelog. A backlog where done items sit next to open ones is a
   backlog nobody reads.
2. **Every open item says what unblocks it.** "Waiting on the operator"
   is an answer only if the question is written down.
3. **Numbers are measured.** Line counts and test counts in this file are
   recomputed by a test; a hand-written number goes stale silently.
4. **A claim nobody checks is a defect in the same class as a bug.** Where
   a document states a fact about the tree, a test recomputes it rather
   than matching a string.
