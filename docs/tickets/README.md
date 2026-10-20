# Context-memory tickets (T65–T70)

**Created:** 2026-10-20, from operator use-case sessions. **Status: design
stage — no code written yet.**

These tickets came out of a design conversation, not from a bug report. They
are written to be implemented, but several of them depend on decisions that
have NOT been made yet. Read this index before picking up any ticket.

## Reading order

1. **This file** — what depends on what
2. **T70** — 🔴 the blocker. Everything else may be rewritten when it lands
3. **T67** — 🟡 the first thing actually to build
4. T65, T66 — blocked on T70's outcome
5. T68 — host work, not code
6. T69 — waiting on operator input

## The dependency problem

**T70 changes the delivery protocol.** Today the bot sends the entire history
in every request (`call_llama.py:790`). T70 proposes sending a single message
plus a session id, with state held by the engine.

If T70 lands, T65 and T66 are substantially rewritten — the question "how many
messages go into the request" becomes "how long does the engine-side session
live". Writing T65/T66 against the current protocol means writing them twice.

**This is why nothing is being implemented yet.** Not caution — arithmetic.

## Ticket map

| Ticket | Subject | State | Depends on |
|---|---|---|---|
| **T70** | Context protocol redesign | 🔴 **blocker** | Strata repo (not yet reviewed) |
| **T67** | Remove `TRIM_HIGH`, stop storing images | 🟡 **ready to build** | — |
| T65 | Reply-to-human: record, don't answer | 🟢 ready | — (T65.1 only) |
| T66 | Time-based context window (7 days) | 🟡 ready | T70, T67.1 |
| T68 | Daily restart 05:00 | 🟡 ready | T66.1 |
| T69 | Bot personality in system prompt | ⏸ waiting | operator input |

## What is safe to build RIGHT NOW

Three items are orthogonal to the T70 decision and will not be thrown away:

- **T67.1** — remove `TRIM_HIGH`. SQLite stays the source of truth in every
  model; a silent data-destroying trim is wrong under all of them.
- **T65.1** — reply-to-human. The semantics don't change, only the delivery path.
- **T69** — personality injection into the system prompt. Orthogonal.

## Open questions

| # | Question | Owner |
|---|---|---|
| 1 | Does Strata have a server-side session API? | operator — repo link pending |
| 2 | `T66.1` — guaranteed minimum of N messages if the window is empty? | operator |
| 3 | `T69` — five personality questions (see ticket) | operator |
| 4 | `T68` — systemd timer vs cron on host | operator |

## Measurements (2026-10-20, llmhost1, Qwen3.8-27B 256k)

Recorded so these are not re-derived later, and so nobody re-panics about
prefill cost without checking.

### Prefill is not recomputed from scratch

`/slots` on the live instance shows `n_prompt_tokens_cache = 0` — which reads
like "no cache at all". That metric is about the on-disk/session prompt cache,
**not** about prefix matching inside the active slot. Measured directly:

```
turn   timing    n_prompt
  1   0.112s        26
  3   0.170s       106
  12   0.171s       474

# same request sent twice:
req 1: 0.94s | 3010 tokens
req 2: 0.08s | 3010 tokens
```

Growing history plateaus at ~0.17 s. Prefix matching is working on this
engine.

### But do not build on it

The operator has measured first-hand that llama's KV slot reuse is poor —
change one byte and it prefills from scratch. And the production engine will
be **Strata**, not llama. An architecture that only works while a particular
engine deduplicates prefixes degrades **silently** on engine swap.

This is why T70 exists. Not because prefill is slow today.

### The real hard limit

`n_ctx = 262144` ≈ 250–300k tokens ≈ **700–900 KB of text**. One megabyte of
history does not fit, cache or no cache. The limit is capacity, not speed.

| History | Tokens | Prefill |
|---|---|---|
| 100 KB | ~25k | ~0.3 s |
| 500 KB | ~125k | ~1.2 s |
| **1 MB** | **~280k** | **exceeds n_ctx** |

### Sessions do not survive an engine restart

```
llama-server flags: --ctx-size --cache-type-k --cache-type-v --cache-ram --parallel
NO --cache-reuse
no --slot-save-path
```

No persistent slots on the current llama. Slots live in process memory. This
does not contradict the assumption that Strata has persistent chats — it means
the design **cannot rely** on it:

> SQLite is the source of truth. The engine-side session is a derived value
> that can be recreated at any moment.

Which is exactly why T67 (no trim) is mandatory: if the session is lost, it
gets rebuilt from SQLite, so SQLite must be complete.

## Files

| File | Contents |
|---|---|
| `CONTEXT-MEMORY-PLAN.md` | Consolidated register of everything, including open questions |
| `BLOCKER-context-message-limit.md` | The original blocker + why it was reformulated |
| `TICKET-T70-context-protocol-redesign.md` | 🔴 The blocker |
| `TICKET-T67-remove-trim.md` | 🟡 First to build |
| `TICKET-T65-context-memory.md` | Reply-to-human + history loading |
| `TICKET-T66-time-based-context-limit.md` | 7-day anchored window |
| `TICKET-T68-daily-restart.md` | Daily 05:00 restart |