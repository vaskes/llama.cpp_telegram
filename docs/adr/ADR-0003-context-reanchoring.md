# ADR-0003 — How the memory window gets re-anchored (T68)

**Status:** Accepted, implemented 2026-10-21
**Context:** T70 (context memory redesign), operator review round
**Supersedes:** nothing. **Related:** T70.1, T70.3, `context_budget.py`, `reanchor.py`

---

## The decision

The daily re-anchor runs in one of three modes, selected by
`BOT_REANCHOR_MODE`:

| Mode | What happens | Needs |
|---|---|---|
| `soft` | The process re-anchors itself on an asyncio timer | nothing |
| `process` | The process is restarted; anchor recomputed at import | ability to restart the bot |
| `process+engine` | As above, plus `BOT_ENGINE_RESET_CMD` runs to drop the engine's KV cache | restart rights + a way to reach the engine |

**Default: `process+engine`.** That is what the operator runs.

`BOT_ENGINE_RESET_CMD` is a free-form shell command, deliberately not a typed
topology. See "Why a free-form command" below.

---

## Why this needs writing down

The operator's framing for T68 was: *"restart bot = restart llama, and that
should be configurable."* On its face that reads as one operation. It is
actually **three unrelated things**, and conflating them produced both a wrong
default and a wrong mental model.

If you remember only one line from this ADR: **the memory window is a
timestamp the bot computes locally. The engine does not participate in it at
all.**

---

## The three concerns, separated

### 1. Re-anchoring the window — bot-side, always sufficient

`CONTEXT_ANCHOR = time.time() - BOT_REMEMBER_DAYS * 86400`. It is computed
inside the bot process. It does not consult the engine, does not depend on the
engine being up, and does not depend on where the engine lives.

Therefore: **no engine restart is ever required for context correctness.**

This is not a theoretical claim. It was checked against the running engine.
`--parallel 1` and the request body (`model`, `messages`, `max_tokens`,
`stream`, `tools`) — there is no `session_id`, no `previous_response_id`, no
Conversations API anywhere. The bot sends the whole conversation every time.

### 2. Restarting the engine — VRAM hygiene

llama.cpp *does* hold server-side state: a KV cache slot, and with
`--keep -1` a retained prompt prefix. The running instance logs:

```
slot get_availabl: id 0 | task 378 | selected slot by LCP similarity,
                   f_sim_best = 0.986 (> 0.100 thold), f_keep = 0.993
```

So "the engine is stateless" is **wrong as stated**, and an earlier draft of
this work said exactly that. The correct statement is narrower: the engine
holds no *conversational* state — no notion of which thread is whose — but it
does cache tokens.

**But LCP matching is precisely the mechanism that prevents corruption.** The
server finds the longest common prefix between the incoming prompt and what is
in the slot, keeps that KV, and recomputes the divergent tail. The feared
outcome — "chaos, duplicates" from a stale KV — is the case LCP exists to
prevent. With a stable system prompt and a window that only grows at the tail,
the prefix matches and the tail is recomputed.

What the growing slot *does* risk is `--keep -1` never releasing the prompt,
so a long-lived slot trends toward the context ceiling and eventually truncates
mid-session (`truncated = 1`). That is a real concern — and it is a VRAM and
slot-lifetime concern, not a context-correctness one.

**Conclusion: restarting the engine is worth doing, for reasons that have
nothing to do with the window.**

### 3. Restarting the bot — an implementation choice

There is no requirement that the bot process restart to re-anchor. The anchor
is a mutable module-level value; `reanchor()` moves it in place. Restarting
the process is simply one way to achieve the same result, and it is the way
that additionally resets everything *else* in the process (leaked memory,
stale in-process caches).

`soft` mode exists because the process restart is the *only* mode that needs
privileges, and a bot published for anyone to run must start without them.

---

## Why `soft` exists (and why it is not the default)

The operator's version — "restart the bot and the engine, with a systemd
dependency when they share a host, a remote command when they do not" —
describes a topology. Topology is the part that varies across forks, so making
topology a *precondition* for re-anchoring means the feature works for the
operator and fails for everyone without systemd or engine access.

That is exactly the wrong coupling. Re-anchoring needs **no privileges at
all**, so requiring privileges to do it is a self-inflicted wound.

`soft` therefore exists so that an operator whose engine is on a machine they
do not control still gets a moving window. `BOT_REANCHOR_MODE=soft` and the
bot does the right thing, forever, unattended.

It is not the default because the operator asked for `process+engine` and gets
that — including the VRAM reset, which `soft` cannot provide.

---

## The rule that must not break

**`soft` mode re-anchors the anchor. It must never turn the window into a
sliding one.**

The sliding-window bug: computing `time.time() - 7 days` per request. The
window then advances continuously, and the bot forgets the beginning of every
live conversation a day at a time — silently, with no error anywhere. Nine
waves of review in this repo have been spent on exactly this class of drift.

Two invariants enforce it, and both are tripwired:

1. **`apply()` never reads the clock.** `ContextBudget.apply()` is pure — no
   I/O, no network, no LLM, no `time.*`. `tests/test_t70_context_budget.py::
   test_apply_does_not_call_time_time`
2. **`CONTEXT_ANCHOR` has exactly two writers:** the module-level assignment
   at import, and `reanchor()`. Any third writer fails
   `test_only_reanchor_writes_the_anchor` and
   `test_anchor_is_a_module_level_constant`.

The existing tripwire was originally stricter — *no* reassignment anywhere. T68
had to widen it to "module level + `reanchor()`". That widening is the whole
risk of this change, which is why the test now asserts the **exact writer
set** rather than merely loosening the assertion.

Also tripwired: the scheduler must not start in `process` modes. If it did, the
anchor would move twice per restart cycle, and in a self-restarting deployment
that is a loop (`test_process_modes_disable_the_in_process_scheduler`).

---

## Why a free-form command, not a typed topology

`BOT_ENGINE_RESET_CMD` accepts any shell command:

```bash
# same host, systemd
BOT_ENGINE_RESET_CMD="systemctl restart llama-qwen5090-256k.service"

# different host, over ssh
BOT_ENGINE_RESET_CMD="ssh -o BatchMode=yes llama-host 'systemctl restart llama-server'"

# engine with an admin endpoint
BOT_ENGINE_RESET_CMD="curl -sf -X POST http://llama-host:8080/admin/reset"

# no engine control at all — valid, and not a failure
BOT_ENGINE_RESET_CMD=""
```

A typed enum (`systemd` / `ssh` / `http`) would need a new case for every
host manager, ssh wrapper and admin API in the wild, and would still miss the
next one. The set of ways to restart something is open-ended; only the empty
case is special, and it is handled as "skip".

Two consequences worth stating plainly:

- **The command is not sandboxed.** It is operator-supplied from their own
  `.env` and runs as the bot user — the same trust boundary as any `ExecStart`
  they wrote in a systemd unit. It must not pretend otherwise.
- **A failing reset does not take the bot down.** `run_engine_reset()` never
  raises and is never called from the request path. A wrong command is logged
  with its output tail and the re-anchor proceeds.

**The systemd dependency the operator asked for is deliberately NOT in this
repo.** Wiring `Requires=` / `After=` / `PartOf=` into a systemd unit is an
operator-topology decision, and this repo cannot own it — the bot may be
running under Docker on someone else's VPS. The unit is expected to be written
by whoever deploys the bot. See `examples/systemd/`.

---

## Interaction with T70.3

T70.3 will summarise out-of-window history **at bot start** — which makes the
re-anchor moment load-bearing for memory quality, not just for tidiness. Two
consequences:

1. **T68 must exist before T70.3.** Without a re-anchor, summarisation never
   runs. This is recorded as a dependency in `docs/TODO.md`.
2. **In `soft` mode, the summarisation trigger must be `reanchor()`, not
   process start.** Otherwise `soft` and `process` modes silently behave
   differently on memory quality while looking identical in config. Written
   down now so T70.3 implements it that way.

---

## Two bugs this design shipped with

Recorded here because this ADR exists to stop the reasoning being undone, and
because both bugs are the *same shape* as the failures this document warns
about. A future change that re-introduces either is re-introducing the class.

### 1. `soft` re-anchored a variable nobody read (`0b6684d`)

`reanchor()` updated the module global `CONTEXT_ANCHOR`. But `get_budget()`
returns the shared `_DEFAULT` instance, and `apply()` filters on *that*
instance's own `.anchor`, assigned once in `__init__`.

The anchor advanced. The generation counter ticked. The log printed
"reanchored". The context window did not move by one second — which means the
mode that exists specifically to re-anchor without a restart did not re-anchor.

The tripwire missed it because it asserted on `CONTEXT_ANCHOR`, the variable the
code was *supposed* to care about rather than the one `apply()` reads. Rule 2
below says `apply()` must not read the clock; it says nothing about which
`anchor` it reads, and nobody asked until it was too late.

### 2. The daily timer fired an hour late, once a year (`4cc6291`)

`seconds_until_next` built its target as `midnight + hour*3600` and added a
flat `86400` when that target was already past. A DST day is 23 or 25 hours.

Measured in `America/New_York`, the day before a spring-forward transition: it
computed `06:00`. Then it got worse — the inner loop re-evaluates every ten
minutes, so at 05:55 it found its own target in the past and pushed it out
another `86400`, skipping a full day.

The docstring already promised "the loop must survive a DST shift". It did not
survive it. Moscow has had no DST since 2014, so the operator never saw it;
a deployment in a DST zone would have.

### What the two have in common

Both **looked healthy while doing nothing**. A green tripwire, a fresh counter,
a reassuring log line, a docstring asserting the property. Nothing in the
source said anything was wrong.

So the standing check for any change in this area is not "does it compute the
right value" — it is *"is the thing that moves the same thing that the thing
being moved controls?"* That question found both. It is also the question a
reviewer should ask first of any new claim here.

---

## Alternatives rejected

**Always restart the process (no `soft`).** Rejected: breaks every fork
without restart privileges, for no benefit. The window moves in-process at
zero cost.

**Make `soft` the default.** Rejected for now: it changes behaviour for every
existing deployment silently, and it cannot do the KV reset the operator
actually wants. Revisit when the fork ecosystem is known to skew toward
container-only deployments.

**Typed restart topology in config.** Rejected: see "Why a free-form command".

**Slide the window per request.** Rejected permanently. This is the failure
mode this whole ADR exists to prevent.

---

## What would make us revisit this

- An engine that keeps *conversational* state server-side (`previous_response_id`,
  Conversations API). Then the engine genuinely participates and the coupling
  the operator originally described becomes correct.
- Observed `truncated = 1` in llama logs, which would mean slots are genuinely
  hitting the ceiling and the KV reset needs to be more frequent than daily.
- The 256k summarisation boundary (deferred by the operator, see `docs/TODO.md`),
  if it turns out to require the raw history to be re-summarised on every
  re-anchor rather than once.