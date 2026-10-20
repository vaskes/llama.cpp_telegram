# Architecture Decision Records

Decisions worth remembering. The test for "worth an ADR": will we come back to
this, and will the next person — or the next session — get it wrong in a way
that is silent?

Context memory is the current hot spot: large-context management is where this
bot's behaviour is least visible from the outside and most likely to drift
silently. Start here when touching it.

| ADR | Decision | Status |
|---|---|---|
| [0003](ADR-0003-context-reanchoring.md) | How the memory window gets re-anchored (T68) | Accepted, implemented 2026-10-21 |

## Why an ADR here at all

This repo has 200+ forks. Most of the decisions that matter are invisible in
the code, because the code cannot enforce them: a window that slides instead of
anchoring produces correct-looking output forever. An ADR is where the *reason*
lives, so the next person does not have to rediscover it from a bug report.

Each ADR also records what would make us revisit it. A decision without a
revisit condition is just a preference with extra steps.
