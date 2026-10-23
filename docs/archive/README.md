# docs/archive/ — closed review cycles and dated artefacts

Nothing in this directory is current. It is kept because it is the record
of *how* the code got here, and because several claims in the live docs
("this was fixed in wave 4, this was rejected in wave 2") only make sense
against the review that produced them.

**Rule for the live tree:** a review document lives in `docs/` only while its
cycle is open. When the cycle closes — findings fixed, findings rejected,
verdict recorded — the document moves here, in the same commit that closes
the cycle. An open cycle's notes live in `docs/review/`; an index of them
is in `docs/README.md`.

| Directory | Cycle | Closed |
|---|---|---|
| `v0.0-v0.4/` | The first two MINIMAX reviews (pre-release era) | 2026-10-13 (T31) |
| `v0.5-v0.12/` | The MINIMAX review cycle: v3, v6, v8, v9, v10 | 2026-10-23 (doc cleanup) |
| `2026-10/` | The 2026-10 context-memory review cycle and the wave-6 close-out | 2026-10-23 (doc cleanup) |

## What is in here

- **`REVIEW-MINIMAX*.md`** — code-review reports from the MINIMAX review
  cycle (v0.5.0 → v0.9.5). Each report reviewed a specific tag and its
  findings were fixed in the following one.
- **`REVIEW-BRIEF-2026-10-22.md`** — the brief handed to an adversarial
  reviewer for the `call_llama` decomposition. It became TECH-DEBT #8.
- **`2026-10-21-wave6.md`** — the close-out note for review wave 6, the
  first of the three waves that preceded `v0.13.0-stable`.
- **`REVIEW-context-memory-block.md`** — the review request and verdict for
  the T65–T70 context-memory block. Verdict: APPROVED, 2026-10-21.
- **`DEPLOY-NOTES-v0.10.2.md`** — the deploy note for the v0.10.2 bundle.
  It documents paths (`/opt/telegram-bot/`) that were still correct then
  and are not now; `scripts/install.sh` is the current source of truth.

## What is NOT here

- **Current state of the code.** That is `CHANGELOG.md` (what shipped),
  `TECH-DEBT.md` (what was measured and deliberately left), `docs/TODO.md`
  (what is open), `docs/tech-debt/` (the detail behind each debt item).
- **Anything a reader is expected to follow as instructions.** A document in
  this directory may describe a deployment layout, a module layout, or a
  command that has since changed. Read it as history, not as instructions.

## Moving something in

Use `git mv`, so the file's history follows it. In the same commit:

1. Update every link to it in the live docs (`docs/README.md` lists them).
2. If it was referenced from `CHANGELOG.md`, update the path there too — a
   historical entry with a dead link is a broken link, and fixing a path
   does not rewrite history.
3. Do **not** rewrite the document's own contents. It is a dated record;
   its line numbers and its claims about the code are correct *for its
   moment*, and editing them destroys the only reason to keep it.
