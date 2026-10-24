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
| HEAD | nineteen commits past the `v0.13.0-stable` tag (trailer cleanup, codebook, the `dev` limits profile, the Stop-button gate, the answer-files feature, the offline replay of it) |
| Production | **the host runs on a generator** — any request to the model overloads it (operator's measurement, 2026-10-24), so neither the bot nor the engine can be exercised there for now. The box also lost power earlier the same day |
| Pytest | `2213 passed, 20 skipped, 1 failed` locally. The one failure is environmental: `test_backtest_deploy_artifacts.py::test_control_unmutated_tree_passes` spawns the system `python3`, which has no pytest in this sandbox (it fails on the clean tree too) |
| CI | `lint.yml`: ruff (advisory) + pytest on Python **3.11** and **3.12** — `2214 passed, 1 skipped` on both at `60e6ab6` |
| Production interpreter | Python **3.11** (`telegram-bot/Dockerfile` pins `python:3.11-slim`) |
| Production modules | 55 `.py` files in `telegram-bot/` |
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
| T70.5 | what happens when the summary itself does not fit the window |
| T68b | the engine keeps a KV cache for context that already fell out of the window, and the reset command is unreachable |
| T66.1 | the guaranteed minimum of the last N messages when the window is empty |
| Live edit-replace | the one user-visible scenario with no automated coverage |

Closing the context-memory tickets closed *a* set of tasks. It did not
close this. Treat the four rows above as one blocker with four faces,
and do not read a green board here as "the context problem is done".
(T70.6 left this table on 2026-10-23: the operator's decision was that
the thinking cap belongs to llama-server, so the bot-side knob was
deleted rather than implemented -- see that entry.)


### T70.5 — what if the summary itself does not fit in the context window?

**Status:** deferred by the operator 2026-10-21.
**Where:** `docs/tickets/TICKET-T70-context-protocol-redesign.md`,
`docs/tickets/README.md` (open question 6).

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

### T66.1 — guaranteed minimum of last N messages when the window is empty

**Status:** open, low impact. The operator's strict rule stands: the
window is anchored, not a sliding count.

Nothing implements a floor. If the anchored window contains no messages,
the context is empty. The operator's answer at the time was "not added" —
this is recorded so it is a decision, not an omission.

### Live edit-replace — the one test that cannot be automated

**Status:** manual-only, by design. Not a gap in coverage.

The edit-replace path (a user edits a message while the bot is answering
it; the bot must rewrite its answer) needs two people, a slow model, and
a human watching real Telegram. An in-process characterisation of the
same code is **not** live coverage and is not labelled as such anywhere.

**What it needs:** one operator session with a second participant. See
the detail doc for the scenario: `docs/tech-debt/10-dispatch-1624-lines-high-open.md`.

---

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

### T-FREEZE-CHUNK-1 — a long answer to a photo or a document is not chunked

**Status:** open since 2026-10-24. Not caused by the answer-files work, and
not fixed by it either.

`handle_text` and `handle_voice` split an answer longer than 4000 characters
into several messages. `handle_photo` and `handle_document` do not: they
hand the whole answer to `send_reply`, and Telegram's own limit is 4096
characters. A long answer to a photo is therefore a `Message_too_long`
error, and the user sees the error instead of the answer.

**Why it was not fixed inside the answer-files change:** it is a different
defect, and fixing it there would have changed the behaviour of two
handlers under cover of another feature -- the thing the CODEBOOK's
"one concern" rule exists to prevent. The feature made the exposure
*smaller* rather than larger: the code blocks now leave the prose and
arrive as documents, so an answer that was mostly code is now mostly a
file, and what is left to overflow is the prose.

**What it needs:** the same 4000-character chunking the other two handlers
use, on the two paths that lack it, and a test that drives a long answer
through both handlers.

### T-FREEZE-AUTO-1 — `scripts/smoke_test.py`

**Status:** open since 2026-10-12 (Qwen's wave-3 recommendation).
**Effort:** small.

The recommended first automation step: a real Telegram API + `getUpdates`
polling script that covers ~80% of the operator checklist with no mocking.
The checklist it would replace is the "operator tests pending" section
that used to live in this file and that pointed at a file which no longer
exists.

Today the closest thing is `scripts/verify.py` — production health checks,
not a Telegram round-trip. It has never been a substitute for this.

### T-FREEZE-DOC-1 — `docs/TROUBLESHOOTING.md` after `v0.10.2`

**Status:** open since 2026-10-12. Low value, low cost.

The troubleshooting guide covers transport, LLM, Whisper, donsetch, Docker,
networking, and group-mode topics. It does not cover anything that arrived
after `v0.10.2`: the context-memory block (`/stats` counters, the
anchored window), the `soft` / `process` / `process+engine` re-anchor
modes, the persona file, or the dispatch split.

### T-FREEZE-AUTO-2 — `tests/test_integration.py` with an `integration` marker

**Status:** open, and **subsumed**. The marker machinery exists
(`pytest.ini` registers `tech_debt`; `asyncio_mode = auto`), and the
suite has grown by an order of magnitude since the recommendation was
written. The sandbox integration tests that *do* need a live side-car
live in `tests/test_shell_integration.py` and are excluded from CI by
name. Writing a second, parallel mechanism is not obviously worth it.

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
live edit-replace scenario, which is still open above.

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
| Review waves 1–8 | `docs/archive/2026-10/`; `docs/review/README.md` |

---

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
