---
tech_debt: 14
marker: tests/test_no_duplicate_definitions.py
marker_kind: tripwire
state: closed
issue: null
---

## 14. Duplicate top-level definitions in `tests/` (closed)

**Measured 2026-10-22.** One test file, `tests/test_blockers.py`, was
1743 lines long and contained **19 copies of the same test**, 60 lines
each.

Python binds the last definition and silently discards the others, so
eighteen of those tests had never executed — and no green run said so.
1115 lines of the file were dead code, invisible to the suite and to the
mutation harness that had already passed against it.

Nobody wrote nineteen copies by hand: the tool that appended test blocks
did not check whether the block was already there. That is the actual
defect, and it is why the fix is a tripwire rather than a cleanup.

**The fix** is `tests/test_no_duplicate_definitions.py`: it fails on any
repeated top-level definition, and on any name bound by two separate
imports in one module.

It found one live case immediately — `Path` imported at line 25 and again
at line 199 of `tests/test_drift_t63_env_names.py`.

**Why this is a ledger item and not a drive-by fix.** The suite's green
was the thing that was wrong. A file can be 1743 lines long, contain
1115 lines that never execute, and pass every check that exists. The
tripwire is what makes that state impossible to reach again silently.

## Related

- Item 8's harness rules (`docs/tech-debt/08-decompose-the-tool-loop.md`)
  apply here too: a mutation that was never applied is not a caught
  mutation, and a test that cannot be made to fail is worse than none.
- The same class appeared in `tests/test_prompts.py` in TECH-DEBT #12
  and in the cycle checker in item 2's graph work.

<!-- current: tests/test_no_duplicate_definitions.py=111 -->
