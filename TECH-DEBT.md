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

## 1. `bot.py` is a god-module of re-exports (HIGH)

**What.** `telegram-bot/bot.py` is mostly comment banners (`# === F3 stage
2: re-export persistence and rating modules`) and `from x import _y`
re-exports. The monolith was split into modules, but the dependency graph was
never untangled, so `bot.py` became the facade that papers over the cycles.

**Why it exists.** The cycles are real. `storage` ↔ `dispatch` ↔ `handlers`
↔ `rating` all want each other at import time. The facade was the cheapest way
to make the split land without a rewrite.

**Cost.** Every new module has to be re-exported here or tests that import from
`bot` break. `bot.py` grows monotonically and nobody can tell what it owns.

**Trigger to fix.** Before the next feature that needs a new cross-cutting
module. Not before.

**How to fix.** Break the cycles with a dependency-inversion pass: extract the
shared types into a `core/` with no upward imports, invert the rest behind
small interfaces, then let `bot.py` become `__init__.py` with an explicit
`__all__`. Do it as one focused change, not incrementally.

---

## 2. Late imports inside functions (HIGH)

**What.** `handlers.py` starts most functions with `import call_llama` or
`from state import ...` inside the function body, to dodge `ImportError` at
startup. Consequence: static analysis (mypy, pylint, IDE navigation) treats
those names as undefined, and a broken import fails at call time rather than
at boot.

**Why it exists.** Same cycles as item 1.

**Cost.** Broken imports become runtime errors instead of startup errors. That
is the worst time to find out.

**Trigger to fix.** Same refactor as item 1. They are the same problem; fixing
one without the other is wasted work.

---

## 3. `call_llama` is a 1600-line function marked DO NOT REFACTOR (HIGH)

**What.** One `async def` holding the tool loop, streaming SSE parsing,
non-streaming fallback, abort handling, backpressure, and fallback text
generation. Cyclomatic complexity in the low hundreds. The source comment says
`DO NOT REFACTOR` — a note left after someone tried and did not finish.

**Why it exists.** It works, and every change to it has historically cost more
than it saved (see the CHANGELOG's hotfix count).

**Cost.** This is where every new feature lands. Each one adds a branch to a
function nobody wants to touch, which is precisely how the complexity got
here.

**Trigger to fix.** When a feature needs to add a *new kind* of thing to the
turn (a new streaming mode, a new tool-transport, a new abort semantic) rather
than another flag. Splitting it for its own sake produces the abandoned
half-refactor the comment warns about.

**How to fix.** Extract the pieces that have no shared mutable state first:
SSE parsing, the non-streaming fallback, the fallback-text builder. Leave the
tool loop intact until it can be expressed as a state machine over an explicit
`(messages, tools, iteration)` state, not by chopping on line counts.

---

## 4. Handlers duplicate the same 80% (MEDIUM)

**What.** `handle_text`, `handle_voice`, `handle_document`, `handle_photo`
each do: semaphore → build history → create abort event → persist →
`call_llama` → rollback on failure in `finally`. Only the middle differs.

**Cost.** The rollback path exists four times. A fix to one that misses another
leaves a message persisted with no assistant reply — the exact failure mode
described in `9a23083`, just via a different route.

**Trigger to fix.** Alongside item 3, or immediately if another handler is
added.

**How to fix.** An `@asynccontextmanager` (`@with_llm_turn`) that owns the
semaphore, the abort event and the DB rollback. Start with the DB half, since
that is the half that is duplicated *and* consequential.

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