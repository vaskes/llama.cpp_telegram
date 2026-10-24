# Context-memory tickets (T65–T70)

**Created:** 2026-10-20, from operator use-case sessions.
**Updated:** 2026-10-21 — T67, T70.1, T70.2, T70.4, T68 implemented, shipped
and verified in production. This index was stale below; the table is now the
source of truth.

These tickets came out of a design conversation, not from a bug report. They
are written to be implemented, but several of them depend on decisions that
have NOT been made yet. Read this index before picking up any ticket.

## Reading order

1. **This file** — what depends on what
2. **T70** — the main ticket: `ContextBudget`. Everything else slots into it
3. **T67** — the first thing actually to build (independent of T70)
4. T65 — reply-to-human (T65.1 independent)
5. T66 — semantics only, implemented inside T70.2
6. T68 — host work, not code
7. T69 — ✅ shipped 2026-10-21

## The constraint that decided the architecture

The operator, 2026-10-20:

> «мы бота пишем в public репо, не только под себя… главная задача llm engine
> быть stateless. А если я послезавтра захочу его со strata переключить на vllm?
> у меня на соседней машине как раз есть и две видеокарты и vllm. И это только я,
> а у нас есть ещё 200+ клонеров. В общем нам нужен универсальный путь»

So: the bot must survive an engine swap by changing nothing but a URL. That
rules out anything engine-side — including the engine holding sessions.

## The design that came out of it

T70 originally proposed engine-side sessions (`previous_response_id`). That was
**rejected** — it ties the bot to one engine's API surface.

The replacement is the pattern the industry converged on in 2026:

> **protect recent N · summarize older · extract durable facts · budget-assemble**

implemented as a client-side `ContextBudget` over plain
`/v1/chat/completions`. It knows nothing about engines, so it works with llama,
vLLM, Strata, or anything else that speaks OpenAI-compatible chat completions.

`vllm-project/agentic-api` was evaluated as the "use the existing solution"
option and rejected — it is a Rust gateway with the Responses API, multi-agent
steering, OIDC and checkpoint limits. Correct principle, wrong weight class for
200+ forks of a small bot. See the T70 ticket for the comparison.

## The dependency problem

**T70 provides the place where the decision is made.** Everything else feeds
it rather than being blocked by it.

T65 (reply-to-human) and T67 (no trim) are independent of T70 entirely and can
be built first.

## Known tech debt — acknowledged, scheduled, not ignored

`TECH-DEBT.md` at the repo root, with the detail in `docs/tech-debt/`.
Fourteen items, **all closed** as of 2026-10-23 — the monolith refactor
this index was waiting on (`bot.py` as a re-export god-module, late imports
inside functions, the 1600-line `call_llama`, duplicated handler
boilerplate) was one problem and got one focused refactor, sequenced after
this blocker closed and before the next feature.

One item kept a standing caveat until 2026-10-24: **live edit-replace was
manual-only** (`TECH-DEBT.md` #10). It needed two people, a slow model, and
a human watching real Telegram. The caveat is gone because the feature is:
it was removed rather than kept as a permanent gap.

---

## Ticket map

| Ticket | Subject | State | Depends on |
|---|---|---|---|
| **T70** | `ContextBudget` — universal context assembly | ✅ **all shipped** (T70.1/2/3/4) | — |
| **T67** | Remove `TRIM_HIGH`, stop storing images | ✅ shipped | — |
| **T65.1** | Reply-to-human: absorb, react, don't answer | ✅ **shipped** | — |
| T66 | Time-based window (7 days) — semantics | ✅ done inside T70.2 | T67 |
| T68 | Daily re-anchor | ✅ code shipped; **timer install is the operator's** | T70.2 |
| **T69** | Bot personality in system prompt | ✅ **shipped** (file-based, configurable) | — |

Shipped in `main`: T70.1, T70.2, T70.4a (overflow), T70.4b (`/stats`),
T67.1–67.3, T68 (`soft`/`process`/`process+engine` + ADR-0003),
T70.3 (summarise), T65.1 (reply-to-human absorb), T69 (persona).
The suite was at 377 tests when this block landed; it is 2103 today.

## Sub-tasks of T70

| | Task | State |
|---|---|---|
| **T70.1** | `ContextBudget` skeleton, config, wiring | ✅ |
| **T70.2** | `window` strategy (the operator's 7-day rule) | ✅ |
| **T70.3** | `summarize` strategy (opt-in, not default) | ✅ |
| **T70.4a** | Overflow handling | ✅ |
| **T70.4b** | `/stats` observability | ✅ |

Order followed: T70.1 → T70.2 → T70.4 → T68 → T70.3 when unblocked.

**T70.3 is not written on purpose.** The operator chose the design (summarise
at re-anchor, same model as the bot, then `{system} + {summary} + {window}`)
and deliberately deferred one case: what to do when even the summary does not
fit in 256k. Writing the code before that answer means rewriting the splitting
strategy afterwards.

## Open questions

| # | Question | Owner | State |
|---|---|---|---|
| 1 | T70.3 — which model summarises? | operator | ✅ **same model as the bot** (2026-10-21) |
| 2 | T70.3 — trigger threshold? | operator | ✅ **once at re-anchor** (2026-10-21) |
| 3 | T66.1 — guaranteed minimum of N messages if the window is empty? | operator | ✅ **decided 2026-10-20 — not adding** (strict rule R-A; the newest message is never dropped, so the context is never empty) |
| 4 | ~~T69 — personality questions~~ | operator | ✅ **answered, shipped 2026-10-21** |
| 5 | T68 — systemd timer vs cron on host? | operator | 🔵 **moot** — ADR-0003 made it a mode, not a choice |
| 6 | **T70.3 — what if the summary itself does not fit in 256k?** | operator | ✅ **answered 2026-10-24** — chunked summaries with a DB marker (T70.5) |

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
change one byte and it prefills from scratch. And the engine will not be llama:
it may be Strata, it may be vLLM, and in 200+ forks it is whatever each fork's
operator runs. An architecture whose speed depends on a particular engine
deduplicating prefixes degrades **silently** on engine swap.

This is why T70 exists. Not because prefill is slow today.

### The real hard limit

`n_ctx = 262144` ≈ 250–300k tokens ≈ **700–900 KB of text**. One megabyte of
history does not fit, cache or no cache. The limit is capacity, not speed.

| History | Tokens | Prefill |
|---|---|---|
| 100 KB | ~25k | ~0.3 s |
| 500 KB | ~125k | ~1.2 s |
| **1 MB** | **~280k** | **exceeds n_ctx** |

### Engine-side state is not portable — another reason to reject it

```
llama-server flags: --ctx-size --cache-type-k --cache-type-v --cache-ram --parallel
NO --cache-reuse
no --slot-save-path
```

No persistent slots on the current llama; slots live in process memory. Even
where an engine does offer them, that is one engine's API — vLLM exposes a
different one (Responses API), and there is no common denominator across 200+
forks.

Which leaves the principle that survives every engine swap:

> SQLite is the source of truth. Any derived context — window, summary,
> whatever — is recomputable at any moment.

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

**T69** (bot personality) shipped as a file-based, configurable feature and
has no ticket file of its own: `telegram-bot/personality.py`, the
gitignored `persona.local.json`, and `personality.json.example` with
`enabled=false` so a fork never inherits somebody else's character.