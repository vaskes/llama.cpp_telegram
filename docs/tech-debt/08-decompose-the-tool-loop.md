---
tech_debt: 8
marker: tests/test_drift_call_llama_names.py, tests/test_d2_toolcalls.py,
  tests/test_d3_finish.py, tests/test_d4_dispatch.py,
  tests/test_llm_stream.py, tests/test_no_duplicate_definitions.py,
  tests/test_nonstream_error.py
marker_kind: tripwire   # the 13 marked tests now pin a closed fix
state: closed
issue: null
---

## 8. Decompose `call_llama`'s tool loop — **CLOSED 2026-10-22**

> Last read 2026-10-22. The remaining block has been read and its shape
> is now specified below; one earlier claim about why it was not cut is
> corrected there.

Split out of TECH-DEBT #3 on 2026-10-21. Item 3 was about *extraction* —
moving code out of a monolith into tested modules — and that is finished:
nine modules, 1078 lines, no uncovered function left in `call_llama.py`.
Its tool loop was left intact on purpose so that two debts would not
hide behind each other. This is that debt.

**Started deliberately ahead of the triggers in "Why not now" below.** The
operator inverted the YAGNI call on 2026-10-22: the cost of leaving a
654-line loop in a public repo grows with everything else, and the repo
is the thing about to grow. That reasoning is preserved unchanged as the
benchmark the decision was made against.

### The criteria, measured

Not "done" — measured, on 2026-10-22.

**2. Each part testable in isolation — MET.** Nine leaf modules, each
driven directly by tests that never build a full `call_llama`. Test count
789 → 1211.

**3. No nested closures deeper than one level — MET, and it was already
true.** `walk_defs` over `call_llama` finds no nested function
definitions at all, closures included. Worth stating because the
`test_t54` anti-pattern this criterion names was in `tests/`, not in the
loop.

**4. Every guard has a test that fires and a mutation that removes it —
MET.** `LoopGuard` 13/13, `parse_tool_args` 14/14, `llm_dispatch` 15/15,
`llm_finish` 12/12, the six followed tripwires 7/7. Every harness runs a
green preflight first, compiles each mutation before running it, and
reports CAUGHT / SURVIVED / ERROR apart — an earlier version reported
"SURVIVED" for mutations that had not even parsed.

**1. An iteration is a named unit — MET, by T8.1.** Every decision inside
an iteration has a name and a module, and the iteration itself is now
`ToolLoop.run_one(iteration)` in `llm_iteration.py` rather than the body
of a `for` statement. The loop that drives it is `ToolLoop.run`, four
lines. Measured after the extraction: `call_llama` 478 lines, the method
285, and `walk_defs` finds no nested definition inside any function body
in either module.

### What is left, precisely

One block: the engine request, `call_llama.py:474`–`753`, roughly 280
lines. It builds the request body, takes the streaming or non-streaming
branch, folds SSE into the four buffers, and returns `content_buf`,
`reasoning_buf`, `accumulated_reasoning`, `tool_calls_buf` and
`finish_reason`.

Everything either side of it is done. The dispatch below is
`llm_dispatch`, the endings above it are `llm_finish`, the argument
screening is `llm_toolargs`, and both response formats are already in
`llm_nonstream` and `llm_sse` — what is left is the call site and the
glue, not the logic.

**What it actually is, now that it has been read.** Three POST attempts
in sequence -- non-streaming, then streaming, then a non-streaming
fallback if the stream errors. Nine terminal `return` sites across four
types (`str`, `None`, `dict`, `NonStreamResult`), and two meanings
mixed together: "here is your answer, leave the loop" and "here is the
engine's response, carry on to the next stage". It also writes loop
state on the way (`saw_unavailable_tool`, `msgs`, `final_fallback`), so
the seam is not a clean one-way data flow. That is the specification for
the extraction: every return is terminal, so the function returns either
a response or a tagged terminal value.

**A claim about this block was wrong and is corrected here.** An earlier
version of this item said the cut was blocked by

    if not locals().get('_ns_handled'):

on the grounds that a `locals()` flag is per-frame and would silently
change meaning when lifted. That was written without opening either
site, and both are in the same frame, inside the same `async with`:

    605:  _ns_handled = True                    # non-streaming branch
    632:  if not locals().get('_ns_handled'):   # streaming branch

Lifted, it becomes an ordinary local. The extraction removes the
`locals()` rather than being blocked by it. Recorded because the failure
is the one this pass exists to remove: an unverified claim, written
confidently, and used as a reason to stop.

**Known gap in the tripwires.** A name bound in one branch and read from
another passes both checks -- name resolution treats any binding in the
function as available, and the None-only rule only sees None-bound
locals. Found by mutation, not by reading. Closing it needs branch-aware
scope analysis, not another heuristic.

### Blocks landed

| Block | Commit | Module | Moved |
|---|---|---|---|
| D1 | `7d84ef4` | `llm_toolargs.py` | 119 |
| D1b | `204e2cf` | `llm_nonstream.py` | 28 + 2 sites deduped |
| D2 | `2335551` | `llm_toolcalls.py` | 36 + 7 |
| D3 | `cf673f6` | `llm_finish.py` | 50 (3 endings) |
| D4 | `024068e` | `llm_dispatch.py` | 133 |

`call_llama`: 1616 → 808 lines in the function, loop 654 → 483.

### Defects this work shipped, all found and fixed

The suite was green through every one of them. Listed because the test
count is not evidence about this work.

- `Nudge` used but never imported (C3) — live.
- `vclient` unresolved since C1 moved its binding — live.
- `saw_unavailable_tool` became a local, `UnboundLocalError` on every
  vision call (C1).
- `bash_command` on list-shaped arguments, `AttributeError` mid-turn
  (D4).
- All three SSE framing guards deleted by the first cut (C2).
- `self.finish_reason` in `__slots__`, documented, never assigned (C2).
- `test_t54` testing its own copy of the code instead of production (C4).

### The generalisable part

The class that cost the most is not any of those. It is: **code that
moves between modules leaves names behind, and neither compilation nor a
green suite can see it.** `py_compile` resolves names when the line
executes.

`tests/test_drift_call_llama_names.py` now covers every extracted module.
When it was first widened it passed everything, because it iterated
`tree.body` and those modules are classes — it was checking nothing.
Found by injecting an orphan into a method body.

And six tripwires were pinned to a *location* rather than a property, so
every extraction broke them. The usual response to a test that fails on
every refactor is to delete it, which takes the real check with it.

---

## Progress, 2026-10-22

One of the three remaining pieces of the loop body is out. Lines
487-556 -- building the request body, logging its shape, resetting the
four stream accumulators -- now live in `llm_request.py` (161 lines).
`call_llama`: 972 lines / 825 in the function -> 917 / 766. The loop:
500 -> 441.

That block was chosen because it is the only one of the three with no
return, no await and no I/O. The other two still hold 13 of the 17
returns in the loop and the two mixed semantics -- "here is the answer,
exit" versus "here is the engine's response, go to the next stage" --
which is what criterion 1 actually turns on.

**Still open:** the streaming branch (was 641-770) and the non-streaming
branch (was 558-640). Neither can move until every return in them is
made terminal and marked, which is the work the criterion describes.

**Two tripwires had to be rewritten, not just extended.** One counted
`"model": MODEL` dict literals and asserted >= 2; the extraction turned
one into a keyword argument and it went red on correct code. Its
replacement counted MODEL loads in the file, went green, and a mutation
replacing `model=MODEL` with `model=""` survived it -- because
`_ensure_model` reads the global twice on its own. It now resolves the
call site and requires the keyword argument to be the global itself.
Worth reading before trusting any count-based check on this function.

Marker: `tests/test_llm_request.py`, `tests/test_b2_extraction.py`.


## Progress, 2026-10-22 (block 3 — T3.2)

The streaming branch and the non-streaming fallback it owns are out, in
`telegram-bot/llm_stream.py`. `call_llama`: 917 -> 794 lines in the
file, 766 -> 641 in the function, the loop 441 -> 317. Everything
between blocks 2 and 3 was glue; what is left in `call_llama` is the
iteration itself.

`StreamOutcome` carries both meanings the branch used to mix. Two of its
six exits are `None` — the Stop button, before the first chunk and
between chunks — so a caller asking `terminal is not None` treats them
as continuations and answers a cancelled question with the previous
iteration's buffers. `is_terminal` is set by the decision and never
inferred, the same rule `NonStreamStep` already carried. Both abort
paths are mutation-verified individually, because they are the two lines
in this module a plausible refactor would delete.

### The import cycle was already there

Adding `llm_stream` failed `test_import_graph` — it had joined a
seven-module runtime cycle. The honest reading is not "one more module
became cyclic": `llm_thinking`'s only back-edge was a lazy
`from dispatch import _stop_button_markup`, and `llm_stream` inherited
it by importing `llm_thinking`.

The comment at that import named dispatch as the owner of the function.
It did not own it. The function has lived in `access.py` since
TECH-DEBT #2, and `llm_thinking` was reaching it through a re-export.
Moving it to a leaf (`llm_markup.py`, 46 lines, one function) closed the
edge for real. The cycle went 7 modules -> 3 -> 2, and `call_llama` and
`llm_thinking` left it entirely. A pre-existing `dispatch <-> webhook`
pair became separately visible once `call_llama` stopped joining them;
it was always a cycle and had never been named on its own.

Recording that as "add llm_stream to the list" would have been a green
tick on a regression, which is the failure mode this whole pass exists
to remove.

### Two empty-choices sites had no overflow guard

`test_t70_context_budget` asked whether a *module* calls
`is_context_overflow` anywhere. It passed on the summarize call site and
on `tools_less_fallback`, neither of which classified anything — because
the streaming fallback in the same file had one. Moving that fallback
out made the tautology visible as a red test.

Both are fixed. The check is now per SITE: every branch that handles an
empty choices list must contain the classification. Five sites, all
guarded. `llm_stream`'s own first draft had the same defect in a
different shape — `if not X and is_context_overflow(...)` followed by a
bare `if not X:` — which satisfies no check that asks whether *this*
branch classified. It is now one guard with the check nested inside.

### Nineteen copies of one test

Cleaning up after the block turned up `tests/test_blockers.py` at 1743
lines containing NINETEEN copies of a single 60-line test. Python binds
the last definition; the other eighteen never ran. 1115 lines of dead
code, invisible to a green suite, to a passing mutation harness, and to
anyone reading the file.

Nobody hand-wrote nineteen. A tool appended a block without checking
whether it was already there, and nothing objected because a duplicate
definition is not an error in Python — it is silently the later one.
`tests/test_no_duplicate_definitions.py` now fails on a test defined
twice, on any top-level definition defined twice, and on a name bound
twice by imports. It found one live instance on arrival: `Path`
imported at line 25 and again at line 199 of `test_drift_t63_env_names.py`.

Worth recording because it is the same shape as everything else here: a
check that could not go red was read as coverage.

### Tripwires that had to be widened, not loosened

Six went red on correct code and all six were pinned to a location
rather than a property. None was deleted; the check they were making
moved with the code:

  * `test_c4_thinking` counted three `push_thinking` sites in
    `call_llama.py`. Two moved to `llm_stream`. It now scans both
    modules and checks a *scope* property rather than a spelling: each
    kwarg must be a plain name bound in the enclosing function. The old
    check demanded `throttle=_throttle`, which is the same property as
    `throttle=throttle` in the new module, so it would have failed the
    extraction instead of catching it.
  * `test_c2_sse` pointed at `call_llama.py` for the SSE framing.
  * `test_d2_toolcalls` and `test_d3_finish` asserted a set of delegated
    names against one file. Each name now names its owner.
  * `test_t70_context_budget` — see above, per site.
  * `test_refactor_graph` said `_stop_button_markup` lives in `access`.
    It did, and that was the problem.

### Verification

`scripts/mutation_harness.py` (block 1/2, 13 mutations) and this
block's 10: **23 mutations, 23 caught, 0 survived.** Each harness runs a
green preflight first, refuses a mutation whose declared site count does
not match what the regex finds, scores a non-compiling mutation INVALID
rather than SURVIVED, scores a hang as a catch, and restores the tree
even when it is killed.

Four of the ten initially survived or were refused, and both survivors
were real:

  * "the between-chunk abort stops declaring itself terminal" survived.
    The test used an always-set abort event, which returned at the
    PRE-STREAM check — the between-chunk branch never executed. A test
    that cannot reach the line it names is a comment.
  * dropping `tool_calls_buf` / `finish_reason` from the fallback's
    return survived. Both are silent: the fallback's own tool calls
    travel in `append_msg`, so nothing looks wrong.

Marker: `tests/test_llm_stream.py` (20 tests),
`tests/test_no_duplicate_definitions.py`.


## Progress, 2026-10-22 (block 4 — T3.3, the last of the three)

The non-streaming branch is out. What moved is the DECISION about a
failed POST — `llm_nonstream.classify_error`, a pure function — and the
`await tools_less_fallback(...)` stays in `call_llama` for the reason C1
gave: a failure in the retry belongs to the handler that knows which
iteration it was in.

`ErrorStep.retry_without_tools` is a flag rather than a message, because
the two branches return different KINDS of thing. One hands back an
await, the other a string destined for Telegram. Collapsing both into
one field is the shape `NonStreamResult` was built to avoid.

### The last `terminal is not None`

    if _ns_step.terminal is not None:
        return _ns_step.terminal

was the last place in the loop that inferred terminality from a value.
It happened to be correct — `outcome == "final"` returns earlier, so the
only `is_terminal=True` reaching it carries a string — but it is the
fragile form, and it sits directly next to the abort path where a
terminal value is legitimately None. Now `if _ns_step.is_terminal:
return _ns_step.assert_terminal()`, the same rule the module has carried
since block 2, applied at the one site that had not adopted it.

A structural tripwire pins it: `call_llama` may not compare any
`.terminal` against None anywhere in the loop.

### Where the branch stands

`call_llama`: 917 -> 796 lines in the file, 766 -> 642 in the function,
the loop 441 -> 318. All three POST attempts now live outside it. What
remains in the function is the iteration: the buffers, the message list,
the turn ledger, the dispatch of one already-screened call, and the
decision of whether to go round again.

Criterion 1 of this item — "an iteration is a named unit" — is the one
still open. `stream_iteration` and `non_stream_iteration` are named and
tested, but they are called from a `for` statement rather than being the
body of one, so the loop's own state (four buffers, `msgs`, the ledger,
`iter_had_real_result`) is still shared mutable scope across all of it.
That is the next piece and it is a different kind of work: not moving
lines, but deciding what an iteration is allowed to own.

### Verification

Third harness, 7 mutations against `classify_error`: 7 caught, 0
survived. Running total across all three: **30 mutations, 30 caught.**
Full suite 1555 passed.

Marker: `tests/test_nonstream_error.py` (14 tests).

## T8.1 — "an iteration is a named unit": MET

Criterion 1 asked for the iteration to be reachable by name, and the
question it turns on is not "which file is it in" but *what is it
allowed to own*. The answer here: an iteration owns its own state and
the loop owns nothing but the counter.

`telegram-bot/llm_iteration.py` (473 lines) holds `ToolLoop`:

- `run_one(iteration) -> IterationResult` is the loop body, verbatim in
  what it does, and now a method a reader can open, a test can drive,
  and a mutation harness can aim at. It was a 285-line stretch of
  reachable-by-`next(...)` AST.
- `run()` is four lines and is the whole of the loop: iterate, read the
  decision, return. `call_llama` is now 478 lines and ends with
  `return await ToolLoop(...).run()`.

The one thing that made this more than moving lines is that the two
halves now pass a value, and the value has to say *which kind* of
ending it is. Two exits legitimately carry `terminal=None` — SIGTERM
and the Stop button — so a loop asking "is `terminal` set?" carries on
past both and ships the previous iteration's buffers to a user who
cancelled. `IterationResult` carries an explicit `is_terminal` for
exactly that reason, mirroring what T3.2 did for `StreamOutcome`.

### What the harness found that the tests did not

15 mutations against the seam, and the first run came back
**13 caught, 1 survived, 1 invalid**. The survivor is the one worth
writing down: dropping `.run()` from `call_llama`'s return left a
`ToolLoop` where an answer belongs, and **333 tests stayed green**.

Every test in the loop's own suite drives `run()` or `run_one()`
directly, so none of them can see whether `call_llama` calls it at all.
A suite that only tests the new unit cannot find the seam the new unit
was created to expose. `test_llm_iteration.py` now also calls
`call_llama` with the engine stubbed and asserts on the value that comes
back — which is the only direction that could have caught it.

That is the T0.4 lesson arriving a second time in a new shape: the
extracted block got its own tests, and they still were not enough,
because they were all pointed inward.

### Verification

Fourth harness, `scripts/mutation_harness_iteration.py`, 15 mutations
against `run`/`run_one` and the value object: **15 caught, 0 survived,
0 refused, 0 invalid.** The first run's survivor is the pair of
`call_llama`-side tests added afterwards.

`tests/test_llm_iteration.py` (17 tests) drives the seam
behaviourally: `run` stopping on a terminal result, treating a `None`
payload as terminal, counting from zero, honouring the cap, returning
the collected fallback when exhausted, and `call_llama` awaiting it.

Full suite **1577 passed, 20 skipped.**

Marker: `tests/test_llm_iteration.py`,
`scripts/mutation_harness_iteration.py`.

Running total across all four harnesses: **45 mutations, 45 caught.**

### Closed

All four criteria are met and measured: criterion 1 by T8.1 above, 2 by
the 15 leaf modules each driven directly by tests that never build a full
`call_llama`, 3 by `walk_defs` finding no nested definition inside any
function body, 4 by four mutation harnesses (45 mutations, 45 caught).
`call_llama` 1616 -> 478 lines; the tool loop is 473 lines in its own
module with a named iteration and a four-line driver.

There is no outstanding sub-block of this item. A reference to a "1.1"
appeared in a stray root-level copy of this document for one commit
(`b8d8c11`) and named no work that exists anywhere in the repository or in
its history. That copy was a duplicate of this file, drifted from it, and
has been removed; this file is the one `TECH-DEBT.md` points at.

<!-- current: call_llama.py=377, llm_iteration.py=571 -->
