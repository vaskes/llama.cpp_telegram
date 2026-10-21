---
tech_debt: 8
marker: none -- 654 lines of tool loop cannot be pinned by a tripwire until the loop is decomposed; that is what this item is for
state: open
issue: null
---

## 8. Decompose `call_llama`'s tool loop (654 lines) — **NOT STARTED**

Split out of TECH-DEBT #3 on 2026-10-21, deliberately. Item 3 was about
*extraction* — moving code out of a monolith into tested modules — and
that is finished: nine modules, 1078 lines, no uncovered function left in
`call_llama.py`. Its tool loop was left intact on purpose so that two
debts would not hide behind each other. This is that debt.

**This is not a proposal to start now.** See "Why not now" at the bottom.

### What

`for iteration in range(max_iter)` — 654 of `call_llama`'s 979 lines, two
thirds of the function. It runs the LLM call, dispatches tool calls,
appends results to `msgs`, decides whether to continue, and on the way
out can make three more LLM calls (streaming fallback, non-streaming
retry, final summarize).

### Why it exists

It works. Four waves of review have gone through this repo and none of
them touched this block. It grew a guard at a time — T32, T32b, T32c,
T38.5, T43, T49, T65 — and each one landed in the right place *at the
time*, which is how a 654-line loop with two distant readers and four
double-written buffers happens.

### Cost

Every new tool, every new abort semantic, every new streaming mode adds a
branch here. It is the only place in the codebase where the tool loop,
the Telegram transport, three fallback paths and the answer-shaping all
have to be understood at once.

### Acceptance criteria — about the resulting shape, not the technique

Stated this way on purpose. A state machine is **one** way to reach them
and is **not** required, and writing the criterion as "make it a state
machine" pins the item to an implementation chosen before anyone looked
at what the loop actually does — which is how TECH-DEBT #3 stayed open
for a year against a criterion that no longer described its own goal.

  1. **An iteration is a named unit with an explicit state.** One object
     or one record holds `(messages, tools, iteration, buffers)` for a
     single pass. No buffer is read by code that did not write it in the
     same unit or an explicit successor.
  2. **Each iteration is testable in isolation.** Constructing an
     iteration state and driving it must not require an engine, a live
     socket, or the 979 lines around it. This is the criterion that the
     extraction work established as the real one: `push_thinking` and
     `_scrub_surrogates` were untestable purely because they were nested,
     and neither a state machine nor a smaller function would have changed
     that on its own.
  3. **No nested closures deeper than one level.** Anything that a test
     needs to reach lives at module scope, or is a method on something
     reachable. A test that duplicates a function because the real one is
     unreachable is a failed criterion, not a workaround — see
     `tests/test_t54_surrogate_sanitisation.py`, which carried a copy of
     `scrub_surrogates` for months.
  4. **Every guard has a test that fires on the situation it exists for,
     and a mutation that removes it is caught.** The five loop guards
     already satisfy this via `tests/test_c3_loop_guard.py`; the bar for
     the new code is the same bar, not a weaker one.

### Must preserve, all measured not imagined

  * `msgs` is both input and output. Every guard appends to it; the
    summarize call re-sends the entire list.
  * `iter_had_real_result` is set inside tool dispatch and read by the
    guard at the tail, ~400 lines later.
  * `content_buf` / `reasoning_buf` / `tool_calls_buf` / `finish_reason`
    have two writers each: the non-streaming branch and the streaming one.
    Collapsing them to one representation is most of the work here.
  * `last_empty` is incremented at three malformed-args rejection sites
    *and* at the tail, and zeroed at the tail when the iteration had a
    real result — which overwrites same-iteration rejections. **Not a bug
    to fix.** It is what stops a model that emits nothing but broken
    JSON. `test_c3_loop_guard.py::test_rejections_are_cleared_by_the_next
    _real_iteration` names it so nobody "fixes" it silently.
  * The five loop guards fire in a specific order that is *not*
    cheapest-first. Sorting them changes which message the user receives
    when two conditions trip on one iteration.

### Risk, stated plainly

MEDIUM-HIGH, and materially higher than anything in TECH-DEBT #3. That
refactor moved code and changed no behaviour; it introduced four defects,
three invisible to a green suite. This one rewrites the control flow of
the only loop in the product that can run fifteen engine requests and
spend twenty minutes doing it.

Budget it as its own project with its own review wave, not as the tail
of a refactor that is already closed.

### Why not now

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