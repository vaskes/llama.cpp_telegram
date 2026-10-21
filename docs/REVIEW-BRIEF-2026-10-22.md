# Review brief — the `call_llama` decomposition, 2026-10-22

For an adversarial reviewer. Ordered so that the highest-risk material
comes first, because that is what a review is for.

**If you read one section, read §2.** It is the list of what this work
broke. Ten defects, three of which reached production, and the test suite
was green through every single one.

---

## 1. What was done, and how far it got

TECH-DEBT #3 was about extraction; it closed at `d89ead2`. Its tool loop
was deliberately left intact and opened as item 8. Fifteen commits later:

| | before | after |
|---|---|---|
| `call_llama` (function) | 1616 lines | 825 |
| top-level loop | 654 lines | 500 |
| leaf modules | 0 | 11 |
| tests | 789 | 1298 |

Item 8 defined its own criterion as a *shape*, not a technique. Measured
on 2026-10-22:

- ✅ **each part testable in isolation** — eleven modules, driven directly
- ✅ **no nested closures deeper than one level** — measured; there are
  no nested function definitions at all in `call_llama`
- ✅ **every guard has a test that fires and a mutation that removes it**
  — five harnesses, all with a green preflight
- ❌ **an iteration is a named unit** — every decision inside one has a
  name and a module; the iteration itself is still a `for` with ~190
  lines of glue

The remaining block is `call_llama.py:486..761`, 276 lines, specified in
[tech-debt/08](tech-debt/08-decompose-the-tool-loop.md). Read that before
proposing the last cut — it has nine terminal returns across four types,
and the reason it is still there is written down.

---

## 2. What this work broke

Every one of these shipped with a green suite.

**Reached production:**

| What | Commit | Effect |
|---|---|---|
| `Nudge` used, never imported | `5245415` (C3) | `NameError`, kills the turn, whenever a tool returns nothing useful twice |
| `vclient` unresolved | since `ac79264` (C1) | `NameError` on the 500 path |
| `saw_unavailable_tool` read before bound | `2335551` (D2) | `UnboundLocalError` on **every** message |
| v0.8.1 "retry without tools on 500" dead | since `ac79264` (C1) | silent — a feature that stopped working, no exception |

**Caught before deploy:**

| What | Commit | Effect |
|---|---|---|
| `saw_unavailable_tool` as a loop local | `ac79264` (C1) | `UnboundLocalError` on every photo |
| all three SSE framing guards deleted | `61ddbaa` (C2) | `[DONE]` sent into `json.loads` on every stream |
| `self.finish_reason` in `__slots__`, never assigned | `61ddbaa` (C2) | documented as a feature; dead |
| `bash_command` on list-shaped args | `204e2cf` (D4) | `AttributeError`, mid-turn |

**Also:** `tests/test_t54_surrogate_sanitisation.py` was testing its own
copy of `_scrub_surrogates` rather than production. A duplicate that
exists *because* the real one was unreachable.

### The pattern, since it is the reviewable thing

Four of the live defects are one mistake: **a symbol that moved, and a
reference left behind.**

- a name whose binding moved to another module (`vclient`)
- a name that never moved but stopped being bound (`Nudge`)
- a name whose binding moved and is now permanently `None` (`r` — the
  dead 500 guard)
- a variable that changed from enclosing scope to local
  (`saw_unavailable_tool`, twice)

The tripwires built for this are:

| File | Property | Harness |
|---|---|---|
| `test_drift_call_llama_names.py` | every name read in `call_llama` resolves | 4/4 |
| `test_loop_flag_order.py` | a loop flag is bound before it is read | 4/4 |
| `test_none_local_deref.py` | a local bound only to `None` is not dereferenced | 2/2, +1 documented gap |

**A gap you should attack.** All three miss a name bound in one branch
and read from another. `test_none_local_deref.py` documents it as an
assertion rather than leaving it implied. Closing it needs branch-aware
scope analysis, not a fourth heuristic.

---

## 3. Where to start

Ranked by how much damage a wrong answer does.

1. **The differential oracles.** `test_d2_toolcalls.py`,
   `test_d3_finish.py` and `test_c1_nonstream.py` each re-implement the
   pre-refactor code verbatim and compare it against the module on
   randomised inputs. If an oracle is wrong, the refactor it validates is
   wrong in the same way. Run them; they are the only thing constraining
   the extractions.

2. **`llm_dispatch.execute_tool`.** The bash branch has a profile gate, a
   late import, and a session id that must be derived from `chat_id` and
   never from the model's arguments. That last one was a PII/cross-chat
   leak in wave 5, and the current code is a refactor of the fix.

3. **`llm_toolargs.parse_tool_args`.** Its accept/reject boundary is a
   deliberate decision with tests written against it, and it changed
   during D4 — non-object parses are now normalised to `{}` rather than
   rejected, because four tests said rejecting them loops forever. Two
   earlier attempts were wrong and are recorded in `204e2cf`.

4. **`test_shell_isolation.py::test_call_llama_has_synthetic_tool_result`.**
   I weakened this during D4 and then found it by mutation. It now reads
   the constant it describes. Worth confirming the repair is sound.

5. **`docs/tech-debt/08`.** It contains a claim of mine that was false
   (`locals()` as a blocker for the last cut), corrected in place with
   the correction visible. Check that the correction is itself accurate —
   the second version of that claim was written from a measurement.

---

## 4. Things I would not take on faith

- **The test count.** 1298 green is not evidence about this code. It is
  evidence that the tests do not reach the seams where all ten defects
  lived.
- **A tripwire that cannot fail.** Four times this session a check
  passed everything because it was looking at nothing: an iteration over
  `tree.body` when the modules are classes; a mutation whose needle did
  not match, counted as a survivor; a periodicity-degenerate control; a
  positional tolerance where a magic number was. Every harness here
  refuses to count an unapplied mutation and separates
  CAUGHT/SURVIVED/ERROR.
- **The live verification.** I wrote a container driver for the loop
  flags and it produced no output at all; I did not debug it and did not
  deploy on the strength of it. What is actually verified is the
  structural ordering in the running image and healthz.

---

## 5. How to run it

```bash
# 3.11, the production interpreter
docker run --rm -v "$PWD:/repo" -w /repo -e BOT_TOKEN=1:fake \
  -e CONVERSATIONS_DB=/tmp/pre.db bot-testimg311 \
  python -m pytest tests/ -q

# 3.12, the CI interpreter
docker run --rm -v "$PWD:/repo" -w /repo -e BOT_TOKEN=1:fake \
  -e CONVERSATIONS_DB=/tmp/pre.db bot-testimg \
  python -m pytest tests/ -q
```

`tests/test_shell_integration.py` needs the bash sandbox and is excluded
from the first command; the second runs it.

Both must agree. The suite once passed on 3.12 for nine PEP 701 f-strings
that were `SyntaxError` on the production interpreter — the whole class of
bug was invisible to the wave that should have caught it.

---

## 6. Standing rules this work reaffirmed

- Ground truth is a real run and a mutation. Not an AST comment, not a
  green pytest.
- A tripwire that cannot be made to fail is worse than none.
- A tripwire that fails on harmless refactoring is also worse than none.
- If the test environment is not the production interpreter, a whole
  class of bug is invisible.
- Backup the database before any data migration.
- A claim nobody checked is a defect in the same class as a bug, and has
  been committed as one three times this session: a pointer to a ledger
  item that does not exist, a commit message quoting a test count that
  had never been run, and the `locals()` blocker above.