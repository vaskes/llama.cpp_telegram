---
tech_debt: 3
marker: tests/test_c1_nonstream.py, tests/test_c2_sse.py, tests/test_c3_loop_guard.py, tests/test_c4_thinking.py
marker_kind: tripwire
state: closed
issue: 2
---

## 3. `call_llama` is a 1177-line function marked DO NOT REFACTOR — **CLOSED 2026-10-21**

**What.** One `async def` holding the tool loop, streaming SSE parsing,
non-streaming fallback, abort handling, backpressure, and fallback text
generation. The source comment said `DO NOT REFACTOR` — a note left after
someone tried and did not finish.

**The "1177-line" figure in this title was wrong, and so was the reason
this item stayed open.**

The title's number was never verified: the function was 1616 lines when
work started. The item's own fix criterion was "express the tool loop as
a state machine over an explicit `(messages, tools, iteration)` state".
That criterion was written as if it were the goal. It was a *means*, and
it was aimed at the wrong target.

Measured, the goal was **testability**: 787 of the suite's tests ran
identically against the refactor and its parent, `call_llama` was covered
on 42 lines out of 1173, and the two functions it could not reach were
`push_thinking` — which runs on every streamed turn and was unreachable
because it was a nested `def` — and `_scrub_surrogates`, which had
accumulated a *duplicate copy inside the test suite* precisely because it
was unreachable. A state machine gives explicit transitions; it does not
by itself make a closure reachable.

### Done

Function **1616 → 979 lines**, file 1616 → **1112**. Nine leaf modules,
**1078 lines**, every function in all of them covered by the suite.

| module | what moved |
|---|---|
| `donsetch.py` (200) | 6 `donsench_*` functions + their module-level MCP session state |
| `llm_messages.py` (83) | `_tag_sender` |
| `llm_tools.py` (99) | `fetch_tools_from_llama` + its private cache + the bash gate |
| `http_clients.py` (87) | `_abortable_post`, `transcribe_voice` |
| `llm_nonstream.py` (173) | the non-streaming iteration's transport and parse |
| `llm_sse.py` (127) | `StreamAccumulator`, the SSE delta merge |
| `llm_loop_guard.py` (142) | the loop guards, the abort/nudge text, the two counters |
| `llm_thinking.py` (131) | `push_thinking` + `ThinkingThrottle` |
| `llm_json.py` (53) | `scrub_surrogates`, `dumps_body` |

Deliberately left inside, because moving them breaks things silently:

  * `_ensure_model` / `_discover_default_model` — rebind the global
    `MODEL`, read at two request-construction sites. Moving either sends
    `"model": ""`.
  * the tool loop itself, and everything it touches.

### Coverage, measured

Function-level coverage over `pytest.main()` with stdlib `trace`
(function counts as covered when any non-comment line in its body ran).
`pytest-cov` line numbers are useless across a refactor; function
identity is not.

    call_llama.py        39/1282 -> 44/1113    uncovered: none
    llm_messages.py       2/  84 -> 25/  84
    http_clients.py       6/  88 -> 27/  88
    llm_tools.py         10/ 100 -> 13/ 100
    llm_nonstream.py         --   -> 72/ 174
    llm_sse.py               --   -> 51/ 143
    llm_loop_guard.py        --   -> 47/ 143
    llm_thinking.py          --   -> 55/ 131
    llm_json.py              --   -> 14/  53

Before this item closed, `call_llama.py` reported two uncovered
functions. It now reports none, and both of them live in tested modules.

### Residual — the tool loop, and where it went

**The tool loop (654 lines) is left as-is on purpose.** It is a separate
architectural scope, unrelated to extraction, and tracked as its own
item so that neither debt hides behind the other. Splitting it for its
own sake is exactly what the original `DO NOT REFACTOR` comment warned
about, and the cost of getting it wrong is measured: this refactor
introduced four real defects, three of them invisible to a green suite,
and none of them needed the tool loop touched.

What a future attempt has to preserve, all of it measured rather than
imagined:

  * `msgs` is both input and output — every guard appends to it, and the
    summarize call re-sends the whole thing.
  * `iter_had_real_result` is set inside the tool-dispatch block and read
    by the guard at the tail, ~400 lines later.
  * `content_buf` / `reasoning_buf` / `tool_calls_buf` / `finish_reason`
    have TWO writers each: the non-streaming branch and the streaming one.
  * `last_empty` is incremented in two distant places (three malformed-args
    rejection sites, and the tail) and zeroed at the tail, which overwrites
    same-iteration rejections. **This is not a bug to fix** — it is what
    stops a model emitting nothing but broken JSON, and a test names it.

### What this refactor broke, on the way

Recorded because a ledger listing only wins is a sales page.

  * `saw_unavailable_tool` was assigned only on the `UnknownToolNameError`
    path; as a local it was an `UnboundLocalError` on every vision call,
    through a green 792-test suite.
  * `self.finish_reason` was in `__slots__`, documented, never assigned.
  * the first SSE cut deleted all three framing guards, so `[DONE]` reached
    `json.loads` on every stream. Compiled. 805 tests passed.
  * a test for `scrub_surrogates` had been passing against its own copy
    for months, which is the strongest argument this item's real
    criterion was reachability and not line count.

### Trigger that actually fired

Not "a feature needs a new kind of thing in the turn". What happened:
three code-reading passes each found a helper whose globals could not be
patched, and the patches kept landing on module attributes nothing read.
The rule that came out is the actual fix — **private module state must not
be re-exported through a facade**, or the next reader patches a second
binding that is never consulted.

<!-- current: call_llama.py=377 -->
