# docs/ — what is here and what is current

Everything under `docs/` is either **live** (describes the tree as it is
today) or **archived** (a dated record — see `archive/README.md`). This file
is the map.

If you are a reviewer or a new contributor: read `README.md` at the repo
root first, then this file, then the document your question points at.

## Live documents

### Start here

| Document | What it answers | State |
|---|---|---|
| [SETUP.md](SETUP.md) | How do I install and run this on a fresh host? | live |
| [ARCHITECTURE.md](ARCHITECTURE.md) | What is inside `telegram-bot/`, and why is it split this way? Also: how a release reaches production (the tag, the CI, the release, the host's deploy script) | live |
| [HARNESS.md](HARNESS.md) | What this project is, in the vocabulary of an agent harness: the twelve components, where each one lives here, what is guarded, what is missing, and what we decided about it | live |
| [AGENT_GUIDE.md](AGENT_GUIDE.md) | Short command reference for an AI agent operating the repo | live |
| [CODEBOOK.md](CODEBOOK.md) | The rules this repo learned the hard way — what a monolith is, and what stops one regrowing | live — a rule marked `AUTO` names a test that runs |
| [KNOWN-LIMITATIONS.md](KNOWN-LIMITATIONS.md) | Which limits are Telegram's, not ours | live |
| [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | Why is the bot not answering? | live |

### Features and their contracts

| Document | What it answers | State |
|---|---|---|
| [BASH-SANDBOX.md](BASH-SANDBOX.md) | `bash_command`: setup, threat model, operator runbook | live |
| [TOOL-ISOLATION.md](TOOL-ISOLATION.md) | The whitelist that keeps `bash_command` safe | live |
| [CALL_LLAMA.md](CALL_LLAMA.md) | The turn: request shape, tool loop, abort ladder | live |
| [WEBHOOK.md](WEBHOOK.md) | Webhook transport: when, how, security, fallback | live |
| [RATING_RULES.md](RATING_RULES.md) | The rating scale and how replies are parsed | live |
| [WELCOME_MESSAGE.en.md](WELCOME_MESSAGE.en.md) / [.ru.md](WELCOME_MESSAGE.ru.md) | The copy the bot greets with | live |
| [SANDBOX-RECOMMENDATIONS.md](SANDBOX-RECOMMENDATIONS.md) | A dated handoff **to the sandbox repo's** maintainer | dated — issued against sandbox v2.3.0, current pin is v2.9.1 (the file door, the privilege mode) |

### Where the work stands

| Document | What it answers | State |
|---|---|---|
| [TODO.md](TODO.md) | What is open right now | live — the only active backlog |
| [ROADMAP.md](ROADMAP.md) | What direction the project takes next | live |
| [../TECH-DEBT.md](../TECH-DEBT.md) | What was measured and deliberately left alone | live — index |
| [tech-debt/](tech-debt/) | The detail behind each ledger item | live |
| [tickets/](tickets/) | The context-memory block (T65–T70) | live — shipped, with open questions recorded |
| [adr/](adr/) | Decisions worth remembering, and when to revisit them | live |

### Review

| Directory | What it holds | State |
|---|---|---|
| [review/](review/) | Notes for the **currently open** review cycle | empty — the last cycle closed just before the `v0.13.0-stable` tag |
| [archive/](archive/) | Closed review cycles and dated artefacts | see [archive/README.md](archive/README.md) |

## The three documents that were allowed to drift, and how they stopped

This repo has 200+ forks and a history of documents that described a tree
that no longer existed. Three arrangements now prevent that:

- **`TECH-DEBT.md` is an index, not a copy.** The detail lives in
  `docs/tech-debt/NN-*.md`; `tests/test_tech_debt_index.py` fails if an
  item exists in one place and not the other, and if the index's measured
  line counts go stale.
- **`docs/tickets/README.md` carries the ticket map.** The individual
  ticket files are dated specifications; the table in the index is current.
- **`docs/review/` is emptied when a cycle closes.** A closed review moves
  to `docs/archive/` with `git mv`, so the live tree never accumulates
  verdicts nobody reads.

The general rule, learned the expensive way: **a claim nobody checks is a
defect in the same class as a bug.** Where a document states a fact about
the tree, a test recomputes it rather than matching a string.

## Naming

- `T##` — tickets from the operator's own backlog (`TODO.md`, `tickets/`).
- `W##-` — findings from review wave ## (waves 1–8, all closed).
- `TECH-DEBT #N` — the ledger item, detail in `docs/tech-debt/`.
- `T53 S1`, `T52 #H1` — historical references to a specific review's
  finding. They are kept where they explain a decision; they are not a
  live index and nothing is filed under them.
