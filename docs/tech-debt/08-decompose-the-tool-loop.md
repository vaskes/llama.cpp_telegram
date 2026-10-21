---
tech_debt: 8
marker: tests/test_drift_call_llama_names.py, tests/test_d2_toolcalls.py
state: open
issue: null
---

The `marker` was a paragraph of progress for most of this item, and the
ledger tripwire caught it: `marker` has to name a file, because it is how
the reconciler decides whether an item is pinned. A paragraph reads as
a filename and fails. Same lesson as the dangling reference above, one
layer down: the field is parsed, and prose in it is not a claim.

## 8. Decompose `call_llama`'s tool loop (654 lines) — **IN PROGRESS**

Split out of TECH-DEBT #3 on 2026-10-21, deliberately. Item 3 was about
*extraction* — moving code out of a monolith into tested modules — and
that is finished: nine modules, 1078 lines, no uncovered function left in
`call_llama.py`. Its tool loop was left intact on purpose so that two
debts would not hide behind each other. This is that debt.

**Started deliberately ahead of the triggers below.** The operator
inverted the YAGNI call on 2026-10-22: the cost of leaving a 654-line
loop in a public repo grows with everything else, and the repo is the
thing that is about to grow. Reasoning below is preserved unchanged,
including the "why not now" that was overridden — read it as the
benchmark, not as the decision.

### What

`for iteration in range(max_iter)` — 654 of `call_llama`'s 979 lines, two
fifths of the function, and the only loop in the product that can run
fifteen engine requests and spend twenty minutes doing it.

### Progress

Blocks landed, each with its own module, its own tests and a
mutation-proven harness:

| Block | Commit | Module | Lines moved |
|---|---|---|---|
| D1 | `7d84ef4` | `llm_toolargs.py` | 119 |
| D1b | `2335551`¹ | `llm_nonstream.py` | 28 + 2 sites deduped |
| D2 | `2335551` | `llm_toolcalls.py` | 36 + 7 |

¹ D1b was `824873b`, amended to `2335551` to fix a commit message the
shell had eaten a phrase out of: backticks inside a `-m` message sent
over ssh run as command substitution, and the sentence between them was
replaced by the shell's error output. Write messages to a file and use
`-F`. An earlier draft of this footnote pointed at a ninth ledger entry
for that lesson. There are eight. tests/test_debt_refs.py now checks
that every numbered reference in this directory resolves.

The loop is 605 lines and `call_llama` is 1043. Test count 789 → 1006
across the C and D blocks, on 3.11 and 3.12.

Remaining: D3 (no-tool-calls and hallucination returns, ~46 lines) and
D4 (the dispatch loop itself, ~152 lines, the largest and the only one
that is genuinely entangled).

### Two live production NameErrors, both from this work

Neither the 1006 tests nor `py_compile` could see either one. They
were found by driving the deployed loop.

**`Nudge` used at line 1036, never imported** — `from llm_loop_guard
import Abort, LoopGuard` dropped it. It fires whenever the empty-call
budget reaches 2, i.e. whenever a tool returns nothing useful twice,
and kills the turn. Introduced by Block C3.

**`vclient` referenced after Block C1 moved its binding** — the
`async with httpx.AsyncClient(...) as vclient` went into
`llm_nonstream.py`; the tools-less 500 fallback stayed behind. It
fires exactly when llama-server's grammar parser rejects a tool call,
which is the only condition that path exists for.

The generalised tripwire is `tests/test_drift_call_llama_names.py`.
Mutation-verified against both. When first widened to all ten extracted
modules it passed *everything*, because it iterated `tree.body` and
those modules are classes — it was not checking anything.

The generalisable lesson, already stated for the v0.8.x tripwires and
now with three independent confirmations: **a refactor that moves code
between modules needs a static check that every name still resolves.**
Compilation is not that check, a green suite is not that check, and a
tripwire that cannot be made to fail is worse than no tripwire.

### Why not now

*Superseded — the operator started this deliberately on 2026-10-22.
Kept as the benchmark the decision was made against.*

None of the triggers below has fired. When one does, this item becomes
worth its risk:

  * a tool loop that must support **parallel** tool calls — the loop is
    strictly sequential today and parallelising it is a rewrite, not a
    change;
  * a need to **roll back an iteration** and retry with a different
    strategy, rather than appending and moving on;
  * the log for a stuck turn becoming unreadable, i.e. you cannot tell
    from the logs which of the four fallback paths actually ran.

Until then a state machine here is YAGNI: it buys explicitness nobody is
currently paying for, at the cost of touching the highest-risk code in a
200-fork public repo.