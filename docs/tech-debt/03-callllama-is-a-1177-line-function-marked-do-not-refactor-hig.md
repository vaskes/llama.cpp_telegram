---
tech_debt: 3
marker: tests/test_c1_nonstream.py, tests/test_c2_sse.py
marker_kind: guard
state: partial
issue: 2          # filled in when the Issue is created
---

## 3. `call_llama` is a 1177-line function marked DO NOT REFACTOR (HIGH) — **PARTIALLY DONE**

**What.** One `async def` holding the tool loop, streaming SSE parsing,
non-streaming fallback, abort handling, backpressure, and fallback text
generation. Cyclomatic complexity in the low hundreds. The source comment says
`DO NOT REFACTOR` — a note left after someone tried and did not finish.

**The "1177-line" figure in this title was wrong.** Measured 2026-10-21: the
function spanned 120–1191, that is 1072 lines, in a 1202-line file. The
original 1177 was never verified by anyone. This is the same claim-propagation
failure that produced a `feature/composition-root` handoff earlier in the
session: a number copied between documents until it stopped matching anything.

**Why it exists.** It works, and every change to it has historically cost more
than it saved (see the CHANGELOG's hotfix count).

**Cost.** This is where every new feature lands. Each one adds a branch to a
function nobody wants to touch, which is precisely how the complexity got
here.

### Done (2026-10-21)

1616 → 1072 lines inside the function; six leaf modules, 769 lines, each with
tests that replay a captured live-engine response:

| module | lines | what moved |
|---|---|---|
| `donsetch.py` | 200 | 6 `donsench_*` functions + their module-level MCP session state |
| `llm_messages.py` | 83 | `_tag_sender` |
| `llm_tools.py` | 99 | `fetch_tools_from_llama` + its private cache + the bash gate |
| `http_clients.py` | 87 | `_abortable_post`, `transcribe_voice` |
| `llm_nonstream.py` | 173 | the non-streaming iteration's transport and parse |
| `llm_sse.py` | 127 | `StreamAccumulator`, the SSE delta merge |

Deliberately still inside `call_llama`:

  * `_ensure_model` and `_discover_default_model` — they rebind the global
    `MODEL`, which is read at two request-construction sites. Moving either
    silently sends `"model": ""`.
  * the tool loop, until it can be expressed as a state machine over an
    explicit `(messages, tools, iteration)` state.
  * every abort guard, including the `try`/`except` whose `except` sits 120
    lines past its `try`, past the loop-state writes.

### Not done

Block C3: the abort ladder (max_iter, identical-call, wall-clock,
hallucination, last_empty) is still five inline `if`s. It is the last piece
and the smallest, which is the usual way a block like this goes unfinished —
it needs its own tests that the ladder fires in exactly the same situations,
including a positive control that returns `True` on the chosen path.

### Three defects the extraction itself introduced, and what caught them

  * `saw_unavailable_tool` was assigned only on the `UnknownToolNameError`
    path. As an enclosing-scope variable that was safe; as a local it was an
    `UnboundLocalError` on every successful vision call. The 792-test suite
    was green throughout — no existing test ran the vision parse end to end.
  * `self.finish_reason` was in `__slots__`, documented, and never assigned.
    Harmless in production because the caller reads it off the returned
    `Delta`, and dead for everyone else.
  * The first C2 cut deleted all three SSE framing guards — prefix check,
    `[DONE]` break, decode catch — so `[DONE]` reached `json.loads` on every
    stream. Compiled, and 805 tests passed.

### Trigger to fix — this one fired

The item said to wait until a feature needs a new kind of thing in the turn.
What actually happened was subtler: three code-reading passes each found that
a helper needed its globals patched, and the patches kept landing on module
attributes rather than reaching the function. The rule that came out of it is
the actual fix — *private module state must not be re-exported through a
facade*, or the next reader patches a second binding that nothing reads.

**How to finish.** Block C3, then a pass over the abort ladder's five guards
for the acquire-before-try window that bit three handlers in W7-H1.