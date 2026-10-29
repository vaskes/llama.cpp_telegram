# docs/review/ — notes for the open review cycle

This directory holds the working notes of the review cycle that is **open
right now**. When the cycle closes — findings fixed, findings rejected with
a reason, verdict recorded — its documents move to `../archive/` with
`git mv`, in the same commit that closes the cycle.

**Currently empty.** The wave-1…wave-8 cycle closed at `12d9a4e`
("production-ready", no further blockers). Its durable artefacts are:

| Document | Where it went |
|---|---|
| Wave 6 close-out | [../archive/2026-10/2026-10-21-wave6.md](../archive/2026-10/2026-10-21-wave6.md) |
| Review brief for the `call_llama` decomposition | [../archive/2026-10/REVIEW-BRIEF-2026-10-22.md](../archive/2026-10/REVIEW-BRIEF-2026-10-22.md) |
| Context-memory block review + verdict | [../archive/2026-10/REVIEW-context-memory-block.md](../archive/2026-10/REVIEW-context-memory-block.md) |
| Qwen review of the 2026-10-29 tree, with the verdicts on every finding | [../archive/2026-10/REVIEW-2026-10-29-qwen.md](../archive/2026-10/REVIEW-2026-10-29-qwen.md) |

## What a reviewer needs, in the order they need it

1. **The tree, not the diff.** A review that starts from the diff finds
   the bugs in the diff and misses the ones lying underneath it. Wave 5
   reviewed three commits, wrote "no blockers", and wave 6 found blockers
   that had been there for four waves.
2. **Run it.** `pytest` plus the mutation harnesses in `scripts/`. A green
   suite is necessary and not sufficient — the suite is largely structural,
   and the three wave-5 blockers were all behaviour over time or across a
   module boundary.
3. **Check the claims.** Every tripwire in this repo is written so that the
   mutation which reverts the fix makes it red. A tripwire that cannot be
   made to fail is worse than no tripwire, and so is one that fails on a
   harmless refactor.

## House rules that came out of the last cycle

- Ground truth is a real run and a mutation — not an AST comment, not a
  green pytest.
- The premise of a finding and its proposed fix are two independent claims.
  Check both. A correct premise makes a bad fix sound right.
- A fix that changes *when* something happens needs a test for the
  invariant **before** the event, not only after it.
- A mutation that hangs is not a caught mutation. It has to go red in
  finite time.
- If the test environment is not the production interpreter, a whole class
  of bug is invisible. The repo pins `python:3.11-slim`; CI runs 3.11 and
  3.12.
