---
tech_debt: 8
marker: tests/test_drift_call_llama_names.py, tests/test_d2_toolcalls.py,
  tests/test_d3_finish.py, tests/test_d4_dispatch.py
state: partial
issue: null
---

## 8. Decompose `call_llama`'s tool loop (654 lines) — **3 OF 4 CRITERIA MET**

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

**Why it is not done here.** Line 621 of the block reads
`if not locals().get('_ns_handled')`. That is a `locals()` lookup used as
a control-flow flag between the streaming and non-streaming branches.
It will not survive being lifted into a function unchanged — `locals()`
is per-frame, so the flag silently changes meaning — and working out
what it actually means is a reading task, not a cut. Beyond that: every
extraction block in this pass shipped at least one defect, and two of
them reached production. A 280-line cut across a `locals()` seam, at the
end of a long session, with less verification capacity than at the start,
is how you ship an incident at 2am.

**When to do it:** with a fresh session and a full mutation round, and
after the `_ns_handled` flag has been read and replaced with something
that can cross a frame boundary.

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