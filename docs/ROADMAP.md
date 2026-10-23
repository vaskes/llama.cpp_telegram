# docs/ROADMAP.md — where this project is going

> Strategic view: **what and why**. The tactical backlog — the actual open
> items — is [`TODO.md`](TODO.md). The two complement each other and only
> one of them is a task list.
>
> **Updated 2026-10-23**, against `v0.13.0-stable` (`12d9a4e`).

---

## Where we are

`v0.13.0-stable` is the first release since `v0.12.0-stable` that carries a
refactor rather than a feature — 191 commits, 107 of them touching
production code. What changed structurally:

- **The monolith is gone.** `dispatch.py` is 345 lines (was 1612),
  `call_llama.py` is 478 (was 1616), `_selftest` is 111 (was 1940),
  `bot.py` is a 124-line facade. Each of those is a ledger item in
  [`TECH-DEBT.md`](../TECH-DEBT.md) with a tripwire pinning it.
- **The context policy is ours, not the engine's.** `ContextBudget`
  assembles the history client-side, so swapping llama for vLLM or Strata
  requires changing a URL and nothing else. See
  [`docs/tickets/`](tickets/) and
  [`docs/adr/ADR-0003`](adr/ADR-0003-context-reanchoring.md).
- **The review cycle is closed.** Waves 1–8, ended at `12d9a4e`
  ("production-ready"). The durable artefacts are in
  [`docs/archive/2026-10/`](archive/2026-10/).
- **The history is no longer destroyed on read.** The lazy trim is gone;
  SQLite is the source of truth and every derived context is recomputable.

The bot runs in production in webhook mode and is deployed from the tagged
tree, not from `main`.

---

## What comes next, in order

### 1 — Three decisions that only the operator can make

Nothing is blocked on engineering here. Each of these is parked on a
question, and the question is written down in
[`TODO.md`](TODO.md#open--waiting-on-a-decision-from-the-operator):

| | The question |
|---|---|
| **T70.5** | The summarise strategy builds one summary from all out-of-window history. What if that summary alone does not fit the context window? |
| **T68b** | `BOT_ENGINE_RESET_CMD` is unreachable in every mode. Should the bot own the daily engine restart, or should it be the operator's cron? |
| **T66.1** | Should there be a guaranteed minimum of recent messages when the anchored window is empty? |

**T68b has an implementation ready to go** — the boundary arithmetic is
already DST-correct — but it means the bot restarts `llama-server` once a
day, so it waits for an explicit go-ahead.

### 2 — The live edit-replace test

The one behaviour that cannot be automated: a human edits a message while
the bot is answering, and the bot must rewrite its answer. It needs two
people, a slow model, and someone watching real Telegram. It has been
deferred since 2026-10-12 and it is the last unverified path in the
dispatch split.

### 3 — Automation that replaces the operator's checklist

`scripts/smoke_test.py`: a real Telegram `getUpdates` round-trip covering
most of the old 23-point checklist with no mocking. The checklist itself
was retired when it was retired from `TODO.md`; the script is what should
replace it.

### 4 — Multi-LLM arbitration in groups

**Still a research spike, still deliberately last.** It was Track 4 of the
2026-10 sprint and it did not move, because everything in front of it was
more valuable. The design questions are unchanged:

- **Which** LLM responds to a given user message — round-robin? topic-aware?
  user-scoped? explicit mention?
- **Per-LLM tuning** — different models have different strengths, so the
  arbiter needs either a per-message quality score or a hard routing rule.
- **Conflict resolution** — when two LLMs both want to reply:
  first-write-wins, score-merge, user-as-king?
- **Transparency** — a header in the reply block (`[Qwen-3.8] [Ornith] …`)
  so users can see who answered.
- **Amplification control** — avoid N×N message growth in a busy group.

The operator's estimate when this was last scoped: 2–3 days of
experimentation before a design is worth writing down. It is a spike, not
a feature ticket — expect it to produce a document, not a release.

---

## Deliberately not planned

- **A web UI** (Telegram WebApp). Deferred to `v0.13+`, and `v0.13.0` is
  now the current release without one.
- **Voice output / TTS.** The bot is text-only on the way out.
- **Multi-tenant operator accounts.** One operator, many bot instances.
  Deferred until there is a commercial reason.
- **Engine-side state.** Rejected on the record: the engine is not ours,
  and 200+ forks run whatever their operator runs. See
  [`docs/tickets/README.md`](tickets/README.md).
- **A second summarising model.** The bot summarises with the same model it
  talks with. Resolved 2026-10-21; no change.

---

## The rules the last cycle established

They are not stylistic preferences — each of them exists because breaking
it cost a bug that reached production behind a green test suite.

1. **Review the tree, not the diff.** Wave 5 reviewed three commits, wrote
   "no blockers", and wave 6 found blockers that had been sitting there
   for four waves.
2. **Ground truth is a real run and a mutation.** Not an AST comment, not a
   green pytest.
3. **A tripwire that cannot be made to fail is worse than none** — and so
   is one that fails on a harmless refactor.
4. **The premise of a finding and its proposed fix are two independent
   claims.** A correct premise makes a bad fix sound right.
5. **A fix that changes *when* something happens needs a test for the
   invariant before the event**, not only after it.
6. **Test on the interpreter you ship.** The repo pins `python:3.11-slim`;
   CI runs 3.11 and 3.12.
7. **A claim nobody checks is a defect in the same class as a bug** — and
   it gets committed as one, usually by the person who wrote the fix.

---

## See also

- [`TODO.md`](TODO.md) — the active backlog.
- [`../TECH-DEBT.md`](../TECH-DEBT.md) — the measured-and-deferred ledger.
- [`docs/README.md`](README.md) — the map of this directory.
- [`../CHANGELOG.md`](../CHANGELOG.md) — what shipped.
