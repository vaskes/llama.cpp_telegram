---
tech_debt: 3
marker: tests/test_c1_nonstream.py, tests/test_c2_sse.py, tests/test_c3_loop_guard.py
marker_kind: guard
state: partial
issue: 2          # filled in when the Issue is created
---

## 3. `call_llama` is a 1177-line function marked DO NOT REFACTOR (HIGH) — **PARTIALLY DONE**

**What.** One `async def` holding the tool loop, streaming SSE parsing,
non-streaming fallback, abort handling, backpressure, and fallback text
generation. Cyclomatic complexity in the low hundreds. The source comment says
`DO NOT REFACTOR` — a note left after someone tried and did not finish.

**The "1177-line" figure in this title was wrong.** The function spans
120–1191 measured on 2026-10-21, and the original number was never verified by
anyone. This is the same claim-propagation failure that produced a
`feature/composition-root` handoff earlier in the session: a number copied
between documents until it stopped matching anything.

**Why it exists.** It works, and every change to it has historically cost more
than it saved (see the CHANGELOG's hotfix count).

**Cost.** This is where every new feature lands. Each one adds a branch to a
function nobody wants to touch, which is precisely how the complexity got
here.

### Done (2026-10-21)

Function: 1616 → **1041** lines. File: 1616 → 1172. Six leaf modules, 911
lines, each with tests that replay a captured live-engine response:

| module | lines | what moved |
|---|---|---|
| `donsetch.py` | 200 | 6 `donsench_*` functions + their module-level MCP session state |
| `llm_messages.py` | 83 | `_tag_sender` |
| `llm_tools.py` | 99 | `fetch_tools_from_llama` + its private cache + the bash gate |
| `http_clients.py` | 87 | `_abortable_post`, `transcribe_voice` |
| `llm_nonstream.py` | 173 | the non-streaming iteration's transport and parse |
| `llm_sse.py` | 127 | `StreamAccumulator`, the SSE delta merge |
| `llm_loop_guard.py` | 142 | the loop guards and the abort/nudge text |

### Not done, and why chopping further would be the wrong move

665 of the remaining 1041 lines are `for iteration in range(max_iter)` — the
tool loop itself. This item's own rule was to leave it intact until it can be
expressed as a state machine over an explicit `(messages, tools, iteration)`
state, **not** by chopping on line counts. That condition is not met, so the
item stays `partial`.

Concretely, what a state machine would have to preserve:

  * `msgs` is both input and output; every guard appends to it and the
    summarize call re-sends the whole thing.
  * `iter_had_real_result` is set inside the tool-dispatch block and read by
    the guard at the tail, four hundred lines later.
  * `last_empty` is incremented in two distant places (three malformed-args
    rejection sites, and the tail) and zeroed at the tail. The zeroing
    overwrites same-iteration rejections — see below.
  * `content_buf` / `reasoning_buf` / `tool_calls_buf` / `finish_reason` have
    TWO writers each: the non-streaming branch and the streaming branch.

Until those are collapsed into one representation, splitting the loop into
functions means threading fifteen parameters through each of them and
keeping four buffers alive across the boundary — which is the shape of the
problem, not a reduction of it.

### The three defects the extraction introduced

Recorded because a ledger listing only wins is a sales page.

  * `saw_unavailable_tool` was assigned only on the `UnknownToolNameError`
    path. As an enclosing-scope variable that was safe; as a local it was an
    `UnboundLocalError` on every successful vision call, through a green
    792-test suite.
  * `self.finish_reason` was in `__slots__`, documented, and never assigned.
    Harmless in production — the caller reads the returned `Delta` — and dead
    for everyone else.
  * The first C2 cut deleted all three SSE framing guards, so `[DONE]` reached
    `json.loads` on every stream. Compiled. 805 tests passed.

### Two findings that outlive the refactor

**`last_empty`'s zeroing is not a bug to fix.** Three rejected tool calls in
one iteration are erased by the tail's `last_empty = 0` when the iteration
had a real result. It looks wrong and it is the behaviour call_llama has
always had. `tests/test_c3_loop_guard.py::test_rejections_are_cleared_by_the
_next_real_iteration` exists so nobody "fixes" it silently.

**The abort guards are not a ladder.** Three of five sit in the loop tail; the
hallucination guard fires before any tool runs and returns a dict while the
rest return strings. Ordering the tail cheapest-first would change which
message the user sees when two conditions fire on the same iteration.

### Trigger to fix — the one that actually fired

The item said to wait for a feature needing a new kind of thing in the turn.
What happened was subtler: three code-reading passes each found a helper
whose globals could not be patched, and the patches kept landing on module
attributes nothing read. The rule that came out is the actual fix — *private
module state must not be re-exported through a facade*, or the next reader
patches a second binding that is never consulted.