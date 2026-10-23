# TECH-DEBT.md — index

**This file is an index.** The detail for each item lives in
`docs/tech-debt/NN-*.md`, which carries YAML frontmatter: `tech_debt`,
`state`, `marker` (the tripwire that pins it), `marker_kind`, `issue`.

**Why this file used to be a duplicate, and why that failed.** Until
2026-10-22 this was a second full copy of every item, maintained by hand
alongside `docs/tech-debt/`. It drifted on four of eight entries while the
`docs/` copies stayed correct — and the drift is what a review report was
built on. A stale claim in a ledger is worse than no ledger: `call_llama`
was described here as "a 1177-line function marked NOT TOUCHED" on the
same day six leaf modules were extracted from it, and a reader had no way
to tell which half was current.

`tests/test_tech_debt_index.py` now fails if an item exists in
`docs/tech-debt/` and is missing here, or if this file describes an item
whose recorded state is `closed`. The copies cannot be edited into
disagreement again without a test going red.

---

## Status

| # | Item | State | Tripwire | Cost while it stands |
|---|---|---|---|---|
| 1 | `bot.py` facade of re-exports | **closed** `2eae2cb` | `test_facade_ownership.py` | — |
| 2 | Late imports inside functions | **closed** `d25aff5` | `test_refactor_graph.py` | — |
| 3 | `call_llama` monolith | **closed** `d89ead2` | `test_c1`…`test_c4` | — |
| 4 | Handlers duplicate the wrapper | **closed** — dangerous half extracted | `test_turn_ledger.py`, `test_turn_scope.py` | — |
| 5 | `print()` instead of `logging` | **closed** `83c5092` | `test_logging_stdout_parity.py` | — |
| 6 | `created_at` from the Python clock | **closed** — decision, see file | `test_t6_db_clock.py` | — |
| 7 | `commit()` per INSERT, no migration txn | **closed** — decision, see file | `test_t7_migration_atomic.py` | — |
| 8 | Decompose `call_llama`'s tool loop | **closed** — `call_llama` 1616→478, iteration is `ToolLoop.run_one` | `test_llm_iteration.py`, `test_drift_call_llama_names.py`, `test_loop_flag_order.py` | 15 leaf modules, 45 mutations, 45 caught |
| 9 | `selftest._selftest` — was 1940 lines | **closed** — `_selftest` 1940→111, 7 leaf modules | `test_selftest_runtime.py`, `test_selftest_blocks.py`, `scripts/mutation_harness_selftest.py` | Runtime harness landed first; 4 defects the extraction found are in the item doc |
| 10 | `dispatch.py` — 1672 → 345 | **closed** — blocks A–G, 40 mutations; ticket target ≤400 met by block G | `test_dispatch_routing.py` | Live edit-replace: manual-only, see detail doc |
| 11 | `config.py` — env inventory | **closed** `fb70469` | `test_env_inventory.py` | — |
| 12 | `prompts.py` — 422 lines of module constants | **closed** — prose in `prompt_text/`, facade 75 lines, 9 mutations | `test_prompts.py` | Dockerfile `COPY` for the package |
| 13 | `handlers.py` shared wrapper | **closed** — blocks A–F, 1464→1316, 48 mutations | `test_handler_middle.py` | ~9 duplicated calls per handler |
| 14 | Duplicate top-level definitions in `tests/` | **closed** | `test_no_duplicate_definitions.py` | — |

Four tripwires were found to be measuring something other than their
property while this was being fixed, and all four are recorded in the
commits that rewrote them: a `MODEL` read count that a `model=""`
mutation survived; a control test pinned to "item 9", which then became
a real item; a substring check that could not tell "asserting a stale
number" from "explaining why it was stale"; and a glob that was not
recursive and hid a whole subpackage. None of them were red when
written. Four more that I wrote tonight were falsified the same way
before they shipped.

---

## Measured, 2026-10-22

Numbers here are from an AST pass over `telegram-bot/*.py`, not from prose:

```
bot.py          124 lines, 4 import blocks, __all__ present
dispatch.py    345 lines, 3 top-level defs  <- was 1612
dispatch_group.py 108 lines, human-to-human reply absorb
dispatch_polling.py   352 lines, startup + getUpdates loop
dispatch_webhook.py   325 lines, PTB assembly + handlers
webhook.py             224 lines, aiohttp server + secret check
dispatch_edit.py      108 lines, the edited-message rewrite
selftest.py     239 lines, _selftest = 111 lines  <- was 1940
call_llama.py   478 lines, ToolLoop owns the iteration, 11 llm_* leaf modules
handlers.py    1316 lines
prompts.py      75 lines, 0 functions (module constants)
storage.py      725 lines, class Storage encapsulated, get_store() sole entry
config.py       349 lines, 24 env names, plus 12 elsewhere in the package

Line counts are splitlines(); the trailing newline is not a line. The
earlier figures in this file came from split("\n") and were each one
high -- which is exactly the class of off-by-one that makes a reader
stop trusting the whole block.
print() calls in telegram-bot/*.py: 0
modules calling get_logger(__name__): 20
```

The largest unit of untested code in the repository is not `call_llama`
— item 3, the one that was flagged for months. It was `_selftest`, at
1940 lines, **2.4x** the size that got the monolith treatment, and it was
absent from item 1's text, which describes moving `_selftest` out of
`bot.py` as though that had resolved it. The move did not shrink it.

That is the sentence as written on 2026-10-22, and it is no longer true.
T1.1 cut `_selftest` from 1940 lines to 111 across seven leaf modules, and
the 1940-line figure below it is the size it *started* from.

---

## Deviations and rejected alternatives

Recorded so the question stops resurfacing at every review.

- **Going past 400 lines.** Item 10's ticket asked for 1612 → ≤ 400.
  **Met, in block G: `dispatch.py` is 345 lines.** It was not for six
  blocks. This entry said "Done: 405" for three waves while the size
  tripwire reported a different number each time the file moved; the
  claim was wrong, not the measurement.

  The move was blocks D/E/F, the routing extraction from block C, and
  then `_handle_reply_to_other_user` → `dispatch_group.py`. The split
  line was never line count: a function moved if it was Telegram surface
  or bookkeeping, and stayed if it picks a handler. What is left is
  `_dispatch_update`, `main` and the concurrency caps.

  The cut did not delete a comment to hit the number. It removed dead
  code: an `_on_signal` closure in `main()` with zero callers, and four
  section markers heading empty sections naming functions that had
  already moved to `dispatch_webhook.py` and `dispatch_edit.py`. The
  ~45 comments the wave-5 and wave-6 fixes added are still there,
  because each one explains a bug that shipped green through four
  review waves precisely because the code looked correct.

  This entry had proposed cutting `main()` instead, into
  `dispatch_startup.py`. That was the wrong cut. `bot.py` does
  `from dispatch import main`, so moving it changes the module's
  public surface and relocates what `test_startup_order.py` and
  `test_polling_integration.py` read. The absorb path reaches the same
  number and leaves the entry point alone.

  Two things are recorded because they cost real time, not because they
  sound cautionary:
  - The dependencies went in as **keyword arguments**, not lazy imports. A
    lazy `from dispatch import ...` works and is exactly what
    `test_import_graph.py` flags: it grew the cycle from six modules to
    seven. A passing import test and a growing cycle are not the same fact.
  - `dispatch_polling` **owns** the Application registry, and the other two
    modules read it through `get_dispatcher()` / `set_dispatcher()`. Three
    writers rebinding an imported name is how you get two copies of one
    truth and a half-refactor that imports fine and reads `None`.
- **ORM / `pypika` for `storage.py`.** `Storage` is already an
  encapsulating class, `get_store()` is the sole entry point, and the 41
  statements are plain SQL. A query builder would not have prevented any
  incident we have actually had here.
- **Structuring `config.py` into `DatabaseConfig` / `LLMConfig` classes.**
  Cosmetic. The one part with real value is name validation, and that is
  item 11, standing alone.
- **Hypothesis for sender-name sanitisation.** `_safe_sender_label` lives
  inside `dispatch.py`; there is no module to test. It becomes testable
  when item 10 moves it out. Naming note: "sanitisation" here means
  normalising a display name, not a security filter — do not conflate the
  two when that extraction happens.
- **Batching INSERTs.** See item 7. WAL is on, writes are a handful of
  rows per message, and batching would add a crash-recovery state machine
  to buy throughput nobody needs.

---

## Wave 5: the class behind three green bugs

`2cebf49` passed 2056 tests and lost 100% of messages in the default
transport. All three blockers were live in code that had been reviewed
four times, and the wave-4 discipline -- mutate the fix, prove it works
-- had been applied to a narrower question than the bug deserved.

**C1** `_spawn_dispatch(upd_dict)` with a keyword-only `dispatch_update`
that the one call site never passed. TypeError on every update, caught,
logged at INFO, and `offset` had already moved past the message, so
Telegram never resends it.

**C2** `global SHUTDOWN_EVENT` inside `dispatch_polling`. It rebinds
*that module's* binding of a name imported from `config`; `llm_iteration`
holds its own, still `None`, and that is where the ToolLoop abort guard
reads. SIGTERM could not reach a running inference. This is TECH-DEBT #1's
own class -- unwritable globals, stale readers -- and it survived there
because one instance was cleaned and the pattern was not named.

**H1** `if app is None: app = build(); await app.initialize()` is
check-then-act across a suspension point. Ten concurrent updates built
ten Applications and ran the boot jobs twenty times, on a box where
summarisation is explicitly sequential because the GPU is single.

The common shape is worth keeping: **every one of these is a behaviour
over time or across a module boundary, and the suite is structural.**
`_bound_names` asks "will this raise NameError"; C2 was "will the reader
see this write", which is a different question it cannot answer in
either direction. `_GLOBAL_MATRIX[3]` is `dispatch_polling.py` line for
line, with `must_be_flagged=False` -- the matrix pinned the bug as
correct, which is the failure mode of every check written from the
previous check's output.

`tests/test_polling_integration.py` drives `_poll_loop` against a fake
httpx client and asserts the update arrives at the dispatch call; it
fails 6/6 on `2cebf49`. `tests/test_late_import_policy.py` checks that
the late-import rule and the modules it covers have both been widened,
and that error paths are not logged at INFO -- both verified by mutation,
both red when the fix is reverted.

## The rule, unchanged

An entry here means: *we know, we chose not to, here is the trigger.* If
you find yourself adding a third entry without removing one, the debt is
growing faster than it is being paid and that is its own finding —
escalate it.

Items 1, 3 and 5 sat open across four reviews while the code underneath
was already fixed. That is what the index-plus-tripwire arrangement above
exists to stop.