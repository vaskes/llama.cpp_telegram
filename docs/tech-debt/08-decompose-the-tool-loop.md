---
tech_debt: 8
marker: tests/test_drift_call_llama_names.py, tests/test_d2_toolcalls.py,
  tests/test_d3_finish.py, tests/test_d4_dispatch.py
state: partial
issue: null
---

## 8. Decompose `call_llama`'s tool loop — **FIRST BLOCK LANDED 2026-10-22**

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

**1. An iteration is a named unit — NOT MET.** Every decision inside an
iteration now has a name and a module. The iteration itself is still a
`for` statement, and about 190 lines of glue remain in its body.

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
