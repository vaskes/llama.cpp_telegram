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
| 9 | `selftest._selftest` — was 1940 lines | **closed** — `_selftest` 1940→111, 7 leaf modules | `test_selftest_runtime.py`, `test_selftest_blocks.py`, `mutation_harness_selftest.py` | Runtime harness landed first; 4 defects the extraction found are in the item doc |
| 10 | `dispatch.py` — 1672 lines | **open** | none yet | Every command is a routing change |
| 11 | `config.py` — env inventory | **closed** `fb70469` | `test_env_inventory.py` | — |
| 12 | `prompts.py` — 422 lines of module constants | **partial** — test exists | `test_prompts.py` | Relocation still to do |
| 13 | `handlers.py` shared wrapper remains | **open** — waits on #8 | `test_finally_hosts.py` | ~9 duplicated calls per handler |
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
dispatch.py    1672 lines, 19 top-level defs
selftest.py     239 lines, _selftest = 111 lines  <- was 1940
call_llama.py   972 lines, call_llama = 825 lines, 11 llm_* leaf modules
handlers.py    1464 lines, 4 handlers at 158-291 lines each
prompts.py      422 lines, 0 functions (module constants)
storage.py      725 lines, class Storage encapsulated, get_store() sole entry
config.py       273 lines, 24 env names, plus 12 elsewhere in the package

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

## Considered and rejected

Recorded so the question stops resurfacing at every review.

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

## The rule, unchanged

An entry here means: *we know, we chose not to, here is the trigger.* If
you find yourself adding a third entry without removing one, the debt is
growing faster than it is being paid and that is its own finding —
escalate it.

Items 1, 3 and 5 sat open across four reviews while the code underneath
was already fixed. That is what the index-plus-tripwire arrangement above
exists to stop.