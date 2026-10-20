# Context-memory tickets (T65–T70)

**Created:** 2026-10-20, from operator use-case sessions. **Status: design
stage — no code written yet.**
**Updated:** 2026-10-20 23:05 — T70 rewritten after the operator established
that the bot is a public repo and the engine must stay swappable.

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
7. T69 — waiting on operator input

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

## Ticket map

| Ticket | Subject | State | Depends on |
|---|---|---|---|
| **T70** | `ContextBudget` — universal context assembly | 🔴 main ticket | — |
| **T67** | Remove `TRIM_HIGH`, stop storing images | 🟡 **ready to build** | — |
| T65 | Reply-to-human: record, don't answer | 🟢 ready | — (T65.1 only) |
| T66 | Time-based window (7 days) — semantics | 🟡 in T70.2 | T67 |
| T68 | Daily restart 05:00 | 🟡 ready | T70.2 |
| T69 | Bot personality in system prompt | ⏸ waiting | operator input |

## Sub-tasks of T70

| | Task | Size |
|---|---|---|
| **T70.1** | `ContextBudget` skeleton, config, wiring | ~150 lines |
| **T70.2** | `window` strategy (the operator's 7-day rule) | ~40 lines |
| T70.3 | `summarize` strategy (not default) | ~120 lines |
| T70.4 | Overflow handling + observability | ~60 lines |

Order: T70.1 → T70.2 → T70.4. T70.3 can be deferred; the default works without it.

## Open questions

| # | Question | Owner |
|---|---|---|
| 1 | T70.3 — which model summarises? same endpoint, cheap one? | operator |
| 2 | T70.3 — trigger threshold: messages, tokens, both? | operator |
| 3 | T66.1 — guaranteed minimum of N messages if the window is empty? | operator |
| 4 | T69 — five personality questions (see ticket) | operator |
| 5 | T68 — systemd timer vs cron on host | operator |

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