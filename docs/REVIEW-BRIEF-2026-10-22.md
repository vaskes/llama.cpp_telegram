# Review brief — monolith decomposition, session 2026-10-22

For the operator's review pass. Written at the point the session ended,
not as a summary of what went right.

## What shipped

`call_llama` went 1616 → 1043 lines; the tool loop inside it went 654 →
605 and is still the thing. Ten leaf modules now own the parts that were
in it. Test count 789 → 1006, green on 3.11 and 3.12.

| Commit | What |
|---|---|
| `fb90456` | live engine fixtures |
| `7179641` `df9ea0e` | B1/B2 extraction |
| `ac79264` | C1 non-streaming → `llm_nonstream.py` |
| `61ddbaa` | C2 SSE → `llm_sse.py` |
| `5245415` | C3 loop guards → `llm_loop_guard.py` |
| `a079e3c` | C4 thinking + json |
| `7d84ef4` | D1 arg screening → `llm_toolargs.py` |
| `2335551` | D1b + D2 → `llm_nonstream.py`, `llm_toolcalls.py` |

## The two things worth your attention first

**1. Two NameErrors reached production. Neither was catchable by
inspection, compilation, or 1006 tests.**

- `Nudge` was used at call_llama:1036 and imported nowhere. Block C3
  dropped it from the import list. It fires when the empty-call budget
  reaches 2 — a tool returning nothing useful twice — and kills the turn.
- `vclient` at call_llama:617 has been unresolvable since Block C1. The
  client binding moved into `llm_nonstream.py`; the tools-less 500
  fallback stayed behind. It fires on a 500 from the grammar parser,
  which is the only condition that path exists for.

Both were found by driving the deployed container. Both were fixed before
the next deploy. The class is now covered by
`tests/test_drift_call_llama_names.py`, mutation-verified.

**2. Every refactor block I have done here has shipped at least one bug
of this kind.** `saw_unavailable_tool` (UnboundLocalError, C1),
`self.finish_reason` in `__slots__` but never assigned (C2), all three SSE
framing guards deleted by the first cut (C2), `test_t54` testing its own
copy of the code instead of production (C4), `Nudge` (C3), `vclient` (C1).
The suite was green through all of them.

Read that as: **the test count is not evidence about this work.** The
evidence is the mutation harness and the live container.

## Things I got wrong, with the shape of the mistake

- **A tripwire that could not fail.** The name-resolution check, widened
  to all ten extracted modules, passed everything on first run — it
  iterated `tree.body`, and those modules are classes. It had checked
  nothing. Found by injecting an orphan name into a method body.
- **Testing the copy, not the thing.** `test_t54_surrogate_sanitisation`
  carried its own `_scrub_surrogates`, with a comment saying why. The
  duplicate existed *because* the real one was unreachable.
- **Substring tripwires.** Three separate times I asserted on a name or
  a marker and the assertion passed on a broken tree — `'"[DONE]"' in src`
  survives deleting the guard because my own comment mentions `[DONE]`.
- **Scope creep in an assertion.** `test_the_caller_does_not_rewrite_the_buffer`
  first banned every indexed assignment in `call_llama` and failed on five
  legitimate ones. The rule had to be the key, not the path.
- **A claim I did not measure.** The D1 commit claimed +38 tests when the
  real figure was +38 from a different base; the count written before the
  suite had ever run was wrong. Amended.
- **Shell ate a commit message.** Backticks in `git commit -m` sent over
  ssh ran as command substitution and deleted a sentence from D2's message.
  Amended with `-F`. Write messages to a file.

## What I would check if I were reviewing this

1. `llm_toolcalls.py` against the pre-D2 loop — the 40 differential
   seeds in `tests/test_d2_toolcalls.py` are the oracle; run them, they
   are the only thing that actually constrains the extraction.
2. `llm_nonstream.py:tools_less_fallback` — its `except` returns a
   message built from `primary_error`, and I moved the message
   formatting into the helper. Worth confirming the runbook greps for
   the old shape.
3. The `msg["tool_calls"]` / `tool_calls = msg.get("tool_calls") or []`
   double assignment in `call_llama`. I preserved it verbatim rather than
   simplifying, because it looks redundant but the second read picks up
   the non-streaming path's value. I am not certain, and I did not
   measure it.
4. Whether the `NonStreamResult` two-outcome contract is still the right
   shape now that the tools-less fallback is also a module.

## Not done

- D3 (no-tool-calls / hallucination returns, ~46 lines) and D4 (dispatch
  loop, ~152 lines, the only genuinely entangled one).
- `RATING_MODE` D1 behaviour — still needs your decision.
- Behaviour when a summary exceeds 256k (T70.5).
- T68 timer install.

## Standing rules this session reaffirmed

- Ground truth is a real run and a mutation. Not an AST comment, not a
  green pytest.
- A tripwire that cannot be made to fail is worse than no tripwire.
- A tripwire that fails on harmless refactoring is also worse than no
  tripwire.
- If the test env is not the production interpreter, a whole class of
  bugs is invisible to the wave that should catch it.
- Backup the database before any data migration.