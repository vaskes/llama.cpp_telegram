# docs/TODO.md — the active backlog

> This is the **only** live backlog in the repo. Everything else that looks
> like a task list is one of:
>
> - `CHANGELOG.md` — what shipped, and when;
> - `TECH-DEBT.md` + `docs/tech-debt/` — what was measured, deliberately
>   left alone, and what would change that answer;
> - `docs/tickets/` — the context-memory block (T65–T70), shipped, with
>   the open questions recorded;
> - `docs/archive/` — dated records, kept for the "how did we get here",
>   not for instructions.
>
> **Updated 2026-10-23**, against the `v0.13.0-stable` tag.
>
> The previous version of this file was last touched 2026-10-13 and had
> accumulated four kinds of rot: a status snapshot from before the
> context-memory rewrite (it said the bot ran v0.9.1), a checklist for an
> operator-test file that no longer exists, a "code freeze" from a
> pre-`v0.10.2` cycle, and eleven ticket entries marked open that had been
> closed for months. It was the same failure the `TECH-DEBT.md` index had:
> a second copy of the truth, maintained by hand, drifting.

---

## State of the tree (measured, not remembered)

| | |
|---|---|
| HEAD | one commit past the `v0.13.0-stable` tag (the co-author trailers) |
| Production | webhook mode, container `tb-bot`, `/healthz` → 200 |
| Pytest | `2110 passed, 1 skipped` (132 test files, minus `tests/test_shell_integration.py`) |
| CI | `lint.yml`: ruff (advisory) + pytest on Python **3.11** and **3.12** |
| Production interpreter | Python **3.11** (`telegram-bot/Dockerfile` pins `python:3.11-slim`) |
| Production modules | 53 `.py` files in `telegram-bot/` |
| In-tree selftest | `selftest.py` (239 lines) + 7 leaf modules, `_selftest` = 111 lines |
| Open review cycle | none — waves 1–8 closed just before the `v0.13.0-stable` tag ("production-ready") |

Sizes are `splitlines()`-based and are recomputed by
`tests/test_tech_debt_index.py`, which fails if the numbers in
`TECH-DEBT.md` go stale.

---

## Open — waiting on a decision from the operator

These are not forgotten; they are parked on a question only the operator
can answer. Each one records the decision that unblocks it.

### T70.5 — what if the summary itself does not fit in the context window?

**Status:** deferred by the operator 2026-10-21.
**Where:** `docs/tickets/TICKET-T70-context-protocol-redesign.md`,
`docs/tickets/README.md` (open question 6).

The `summarize` strategy builds a synthetic `user→assistant` summary pair
from history older than the anchor window. That summary is built from
*all* of the old history. If the summary alone exceeds the engine's
context window, something has to give, and the four candidates are not
equivalent:

- split the old part into several summaries;
- drop the oldest facts;
- bound by freshness inside the old part;
- accept the overflow and say so honestly.

**Why it is not urgent:** `T70.3` works without it. The question only
arises at a scale the deployment has not reached — the current history
fits in the window, so nothing is summarised into a corner.

**What unblocks it:** the operator picks one of the four, or states a
fifth. Until then the summarise path stays opt-in and off by default
(`CONTEXT_STRATEGY=window`).

### T68b — `BOT_ENGINE_RESET_CMD` is unreachable in every mode

**Status:** deferred by the operator 2026-10-22, as part of a related
group rather than as a standalone defect.
**Where:** `docs/adr/ADR-0003-context-reanchoring.md`.

Two mutually exclusive gates. `start_reanchor_scheduler()` returns
immediately when `SOFT_REANCHOR_ENABLED` is set — that is, whenever the
mode is not `soft` — so in `process+engine` there is no in-process
scheduler at all. And inside the loop the engine reset only runs when
`REANCHOR_MODE == "process+engine"`. The two sets do not intersect.

The command is needed exactly in `process+engine`, which is the default:
the bot restarts the operator on a schedule, and the engine keeps holding
a KV cache for context that has already fallen out of the window. Nobody
can execute it.

**In production `BOT_ENGINE_RESET_CMD` is not set**, so the wiring is dead
but harmless: there is nothing to execute.

Two options, both waiting on an operational decision:

- **(a)** a second in-process task that sleeps until the same boundary
  (`BOT_REANCHOR_AT`) and calls `run_engine_reset()` in `process+engine`.
  The boundary arithmetic already exists and is DST-correct
  (`seconds_until_next`); it was written for the other loop.
- **(b)** move the command into the operator's cron/systemd unit and drop
  `BOT_ENGINE_RESET_CMD` from `.env.example` and ADR-0003.

(a) is technically cleaner — the bot already owns the boundary computation
and the setting is advertised as the bot's own. But it is an operational
decision: the bot would restart `llama-server` once a day and stop
answering for the duration. **Implementation of (a) needs the operator's
explicit go-ahead.**

### T66.1 — guaranteed minimum of last N messages when the window is empty

**Status:** open, low impact. The operator's strict rule stands: the
window is anchored, not a sliding count.

Nothing implements a floor. If the anchored window contains no messages,
the context is empty. The operator's answer at the time was "not added" —
this is recorded so it is a decision, not an omission.

### Live edit-replace — the one test that cannot be automated

**Status:** manual-only, by design. Not a gap in coverage.

The edit-replace path (a user edits a message while the bot is answering
it; the bot must rewrite its answer) needs two people, a slow model, and
a human watching real Telegram. An in-process characterisation of the
same code is **not** live coverage and is not labelled as such anywhere.

**What it needs:** one operator session with a second participant. See
the detail doc for the scenario: `docs/tech-debt/10-dispatch-1624-lines-high-open.md`.

---

## Open — code, not blocked on anyone

### T-FREEZE-AUTO-1 — `scripts/smoke_test.py`

**Status:** open since 2026-10-12 (Qwen's wave-3 recommendation).
**Effort:** small.

The recommended first automation step: a real Telegram API + `getUpdates`
polling script that covers ~80% of the operator checklist with no mocking.
The checklist it would replace is the "operator tests pending" section
that used to live in this file and that pointed at a file which no longer
exists.

Today the closest thing is `scripts/verify.py` — production health checks,
not a Telegram round-trip. It has never been a substitute for this.

### T-FREEZE-DOC-1 — `docs/TROUBLESHOOTING.md` after `v0.10.2`

**Status:** open since 2026-10-12. Low value, low cost.

The troubleshooting guide covers transport, LLM, Whisper, donsetch, Docker,
networking, and group-mode topics. It does not cover anything that arrived
after `v0.10.2`: the context-memory block (`/stats` counters, the
anchored window), the `soft` / `process` / `process+engine` re-anchor
modes, the persona file, or the dispatch split.

### T-FREEZE-AUTO-2 — `tests/test_integration.py` with an `integration` marker

**Status:** open, and **subsumed**. The marker machinery exists
(`pytest.ini` registers `tech_debt`; `asyncio_mode = auto`), and the
suite has grown by an order of magnitude since the recommendation was
written. The sandbox integration tests that *do* need a live side-car
live in `tests/test_shell_integration.py` and are excluded from CI by
name. Writing a second, parallel mechanism is not obviously worth it.

### T-FREEZE-AUTO-3 — a deploy workflow (`.github/workflows/deploy.yml`)

**Status:** open, **needs an operator decision and a secret.**

CI/CD that deploys on tag push. It needs a deploy SSH key in the
repository's Actions secrets, and the deployment topology (compose file
path, `.env` handling, volume mounts) is the operator's, not the repo's.
There is no secret configured for it today.

---

## Accepted as documented risk — not a bug, not a task

### T-FREEZE-1 — the semaphore LRU can evict a held semaphore

`_user_semaphores` is an LRU capped at `_USER_SEMAPHORES_MAXSIZE`, and the
eviction does not check whether the semaphore it drops is currently held.
The next message from that user therefore gets a *fresh* semaphore with
the full `_PER_USER_SEMAPHORE_LIMIT` budget, so the per-user concurrency
cap can be exceeded.

Accepted for the current deployment: the whitelist is short, so the dict
holds a handful of entries and never approaches the 500-entry cap
(`_USER_SEMAPHORES_MAXSIZE`). Revisit if the deployment scales to many
users or if LOCKDOWN is relaxed.

`state._get_user_semaphore` is the function; the cap is
`_PER_USER_SEMAPHORE_LIMIT` in the same module.

### T-FREEZE-2 — `call_llama` was a god function

**Closed, differently.** It was not "observed in production long enough":
it was measured, split, and measured again. `call_llama.py` is now 478
lines with 17 `llm_*` leaf modules, and the tool loop is `ToolLoop.run_one`
/ `ToolLoop.run`. See `TECH-DEBT.md` items 3 and 8.

### T-FREEZE-3 — `BOT_COMMANDS` mixes Russian and English

Cosmetic and intentional: Russian descriptions with English technical terms
("sub-talk"). Left as-is.

### T-FREEZE-4 — `_RATING_PREFIX_RE` / `_EMOJI_CHAR_RE` were unified

False alarm — the Qwen report was wrong. Already correct.

### T-FREEZE-MT-1..23 — the operator's manual checklist

Executed against `v0.10.2` by the operator and a second tester (Vaskes and
Dimon, 2026-10-12). The only item that was deliberately not done is the
live edit-replace scenario, which is still open above.

---

## Closed in the current cycle — where to read it

Not repeated here; each line points at the record.

| Work | Record |
|---|---|
| Context memory T65–T70 | `CHANGELOG.md` § v0.13.0-stable; `docs/tickets/README.md` |
| `dispatch.py` 1612 → 345 | `TECH-DEBT.md` #10; `docs/tech-debt/10-*.md` |
| `call_llama` 1616 → 478 | `TECH-DEBT.md` #3, #8 |
| `_selftest` 1940 → 111 | `TECH-DEBT.md` #9 |
| `bot.py` → 124-line facade | `TECH-DEBT.md` #1 |
| `handlers.py` shared wrapper | `TECH-DEBT.md` #13 |
| `prompts.py` prose → `prompt_text/` | `TECH-DEBT.md` #12 |
| Env-var name validation | `TECH-DEBT.md` #11 |
| Review waves 1–8 | `docs/archive/2026-10/`; `docs/review/README.md` |

---

## House rules for this file

1. **A task lives here or nowhere.** If it is not open, it is in the
   changelog. A backlog where done items sit next to open ones is a
   backlog nobody reads.
2. **Every open item says what unblocks it.** "Waiting on the operator"
   is an answer only if the question is written down.
3. **Numbers are measured.** Line counts and test counts in this file are
   recomputed by a test; a hand-written number goes stale silently.
4. **A claim nobody checks is a defect in the same class as a bug.** Where
   a document states a fact about the tree, a test recomputes it rather
   than matching a string.
