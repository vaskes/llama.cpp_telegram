# TECH-DEBT.md — known debt, deliberately not fixed yet

**Created:** 2026-10-21, after the Qwen architectural review.
**Status:** acknowledged and scheduled, not ignored.

This file exists so the debt below is a decision on record rather than an
oversight. Each item says why it was NOT fixed now, what it costs while it
stands, and what the trigger is to fix it.

---

## The rule for this file

An entry here means: *we know, we chose not to, here is the trigger.* If you
find yourself adding a third entry without removing one, the debt is growing
faster than it is being paid and that is its own finding — escalate it.

---

## 1. `bot.py` is still mostly a facade of re-exports — PARTIALLY DONE 2026-10-21

**I marked this DONE and it was not.** I had moved the 1894-line `_selftest()`
out and called the item closed on that basis. The 1894 lines were the easy
part; the facade was the actual item and it was still there.

**What actually landed:**

- `bot.py` 2245 → 261 lines.
- `_selftest()` moved to `selftest.py`, unchanged.
- Six dead duplicates deleted, after confirming nothing outside `bot.py` read
  them: `_SUBTALK_NAME_RE` and `_TOPIC_NAME_RE` (owner: `chat_utils.py`),
  `_LLM_TOKEN_RE` (owner: `prompts.py`), `_user_semaphores` and
  `_global_llm_sem` (owner: `state.py`), and `_detect_tool_intent`, whose own
  docstring said "removed next refactor" and which returned `True`
  unconditionally. Seven unused imports went with them.
- `_user_semaphores` / `_global_llm_sem` were not merely duplicated: they were
  *unwritable*. `state.py` rebinds its own module globals inside its accessors,
  so any holder of the imported name was holding a stale reference. The copy
  in `bot.py` could never have been the live one.

**What is still open, enumerated so "PARTIALLY DONE" is not mistaken for
"nearly done":**

1. **~15 `from X import (...)` blocks** (config, prompts, persistence, rating,
   call_llama, dispatch, handlers, state). Roughly 100 of the 261 lines.
2. **The entry point**: `python -u bot.py` in the compose command, plus
   `main()` under `__main__`.
3. **Fork compatibility**: `selftest.py`'s own docstring says forks patch
   `bot.BOT_COMMANDS`, so at least some of the re-exports are deliberate public
   API rather than residue.
4. **`bot.store`** — a module-level singleton captured at import time.

**Correction to my own earlier claim.** I wrote that the facade was
"load-bearing" because `dispatch.py` imports `bot` at module level. I did not
check. Nothing in `telegram-bot/` imports `bot` at module level at all, and the
module-level import graph has been acyclic since `b31bff3`. What remains is a
script that happens to re-export things for forks. `tests/
test_facade_ownership.py::test_bot_is_a_script_not_a_library` fails if a
module-level `import bot` reappears, so the claim cannot silently become true
again.

**Trigger to finish.** Before the next feature that adds a cross-cutting
module — this is the original trigger, unchanged. To finish: pick the public
API the forks actually depend on, move it behind an explicit `__all__`, and
repoint `selftest.py` and the test suite at the owning modules. Most of the
suite already does.

---

## 2. ~~Late imports inside functions~~ ✅ DONE 2026-10-21

**Was.** 8 late `from dispatch import X` inside handlers.py function bodies.
The module-level graph had **zero** cycles — the cycle existed only because of
those eight lines. Everything they reached for turned out to be leaf code, so
it moved to `chat_utils.py` and `access.py` and handlers now imports nothing
from dispatch at runtime.

**Now.** The module-level import graph is acyclic. Pinned by
`test_refactor_graph.py`: no cycles, no late imports back into the old cycle,
leaf modules stay leaves. Verified that re-introducing one late import fails
three of those tests.

**One breakage worth remembering:** `test_dispatch_start.py` patched
`dispatch.is_authorized` to bypass the whitelist. That patch point stopped
working the instant the function moved, because it resolves the name in its
*own* module's globals — so the test would have kept passing while no longer
bypassing anything. That is worse than no test, and it is the shape a move
like this always takes.

---

## 3. `call_llama` is a 1177-line function marked DO NOT REFACTOR (HIGH) — **NOT TOUCHED**

**What.** One `async def` holding the tool loop, streaming SSE parsing,
non-streaming fallback, abort handling, backpressure, and fallback text
generation. Cyclomatic complexity in the low hundreds. The source comment says
`DO NOT REFACTOR` — a note left after someone tried and did not finish.

**Why it exists.** It works, and every change to it has historically cost more
than it saved (see the CHANGELOG's hotfix count).

**Cost.** This is where every new feature lands. Each one adds a branch to a
function nobody wants to touch, which is precisely how the complexity got
here.

**Deliberately not started on 2026-10-21**, after items 1, 2 and 4 landed.
This is the one item where a half-refactor is worse than none, and it needs a
person who can watch the bot answer messages while it happens. The other three
were mechanical; this one is not.

**Trigger to fix.** When a feature needs to add a *new kind* of thing to the
turn (a new streaming mode, a new tool-transport, a new abort semantic) rather
than another flag. Splitting it for its own sake produces the abandoned
half-refactor the comment warns about.

**How to fix.** Extract the pieces that have no shared mutable state first:
SSE parsing, the non-streaming fallback, the fallback-text builder. Leave the
tool loop intact until it can be expressed as a state machine over an explicit
`(messages, tools, iteration)` state, not by chopping on line counts.

---

## 4. ~~Handlers duplicate the same 80%~~ 🟡 MOSTLY DONE 2026-10-21

**Done.** The consequential half: the persist/rollback block existed eight
times (four handlers × abort path and exception path). `turn.py` owns it now.
Control flow untouched. 10 tests, mostly about failure *direction* — an undo
that does not run leaves half a conversation, so persisting sets the flag
before awaiting, and a failed undo logs at ERROR with its consequence spelled
out.

**Still duplicated.** The semaphore → history → abort_event → call_llama
scaffolding itself. `handle_text`, `handle_voice` and `handle_document` remain
structurally near-identical for ~120 lines each.

**Why not finished.** Finishing it means restructuring three 200-line handlers
into a single template function, which is real control-flow surgery — and the
one remaining benefit is readability, not correctness. The rollback, which is
where a mistake costs a user message, is already in one place.

**Trigger.** When item 3 lands and `call_llama` gains a stable call signature
worth templating against. Until then the duplication is bounded and the
dangerous part is extracted.

---

## 5. `print()` instead of `logging` (MEDIUM)

**What.** Every module logs with `print(..., flush=True)`.

**Why it exists.** The compose file pipes stdout through `tee -a bot.log`, so
output lands somewhere, and the format is greppable.

**Cost.** No log levels, so the one line that matters cannot be turned up
without turning up everything. No structured fields, so correlating a failure
across the bot, the sandbox and the engine means grepping. On a busy group the
useful line is buried in thousands of identical shapes.

**Trigger to fix.** When log volume becomes a problem in practice — i.e. when
someone actually needs to filter. Doing it earlier means reformatting every
line and re-reading every `grep` in a runbook for no observed benefit.

**How to fix.** A `logging.getLogger(__name__)` shim that keeps `flush=True`
and the existing `[prefix]` text, so the migration is mechanical and the
existing greps keep working. Do it as one sweep, not incrementally — half
migrated is the worst state.

---

## 6. `created_at` is `time.time()`, not a DB clock (MEDIUM)

**What.** Timestamps come from the Python process clock.

**Why it stays.** Correct ordering today. `CURRENT_TIMESTAMP` would make
`context_budget`'s age filter depend on SQLite's clock and formatting
conventions, and would complicate the tests that pin window behaviour against a
faked Python clock — which is the whole reason the age tests are cheap.

**Cost.** An NTP step or a VM migration rewrites ordering. `ORDER BY id`
already exists as the stable tiebreaker, so the blast radius is the *age
filter*, not the history order: a backwards clock step would make recent
messages look a week old and drop them from the window.

**Trigger to fix.** If a clock incident is ever observed, or if the bot is
deployed somewhere with aggressive time correction. Until then it is a
theoretical risk with a real blast radius, which is the honest description.

**Mitigation available today:** `reanchor()` refuses to move the anchor
backwards in effect (a stale summary is kept rather than discarded precisely
because the anchor only moves forward), and `ContextBudget._keep` tolerates
unparseable timestamps by keeping the message.

---

## 7. `commit()` per INSERT, no migration transaction (LOW)

**What.** Every write commits. `_migrate_v1_to_v2` runs `ALTER TABLE` in a loop
with per-statement error handling that logs a warning and continues.

**Why it stays.** WAL is already on, and the bot writes a handful of rows per
message. Batching would buy throughput nobody needs and would cost a second
state machine around crash recovery.

**Cost.** Under real write concurrency SQLite can throw `database is locked`
(mitigated by WAL plus `check_same_thread=False`, not by batching). And a
migration that fails halfway leaves a partially-migrated schema with only a
warning in the log.

**Trigger to fix.** When the second generation of forks appears on the same
schema, or when a migration touching existing rows lands.

---

## Fixed, kept here as context

- **Bash rate-limit "default" bucket** — reported in the same review, did not
  reproduce. `session_id` is always `chat-{chat_id}` (T57 #S16), so the branch
  is unreachable. Recorded so nobody re-reports it.
- **WAL not enabled** — already on in `storage._conn()`.

---

## What is NOT tech debt

Tripwires. The AST tests look like scaffolding but they are load-bearing: two
of the bugs found in this block (`0b6684d`, `4cc6291`) would have been caught
by a human reading the code, and one of them was caught by a tripwire that
itself had to be rewritten to mean the right thing. They are cheaper than the
incidents they replace.