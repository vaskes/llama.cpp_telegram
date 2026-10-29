# Code review request — context-memory block (T65–T70)

**Branch:** `main` · **Base:** `8069781` (update.sh fix)
**Scope:** 15 commits, 41 files · **445 tests passing**

---

## Already found and fixed — do not re-report

Two bugs were found and fixed *before* this review was handed out. They are
listed here so the review does not spend its budget rediscovering them, and so
that a fix landing by accident is still visible.

### 1. `reanchor()` moved a variable nobody read (`0b6684d`)

It updated the module global `CONTEXT_ANCHOR`, but `get_budget()` returns the
shared `_DEFAULT` instance and `apply()` filters on *that* instance's own
`.anchor`, assigned once in `__init__`. So the anchor advanced, the generation
counter ticked, the log said "reanchored" — and the context window did not move
by a second. `BOT_REANCHOR_MODE=soft` was a silent no-op, which is the one mode
that exists specifically to re-anchor without a restart.

The original tripwire missed it because it asserted on `CONTEXT_ANCHOR` — the
variable the code was *supposed* to care about, rather than the one `apply()`
reads. That is the claim-vs-implementation gap this block of work exists to
prevent, committed inside the module that prevents it.

### 2. The daily timer fired an hour late, once a year (`4cc6291`)

`seconds_until_next` built its target as `midnight + hour*3600` and added a flat
`86400` when that was already past. A DST day is 23 or 25 hours. Measured in
`America/New_York`: the day before a spring-forward transition it computed
`06:00` instead of `05:00`, then the inner 10-minute re-evaluation found its own
target in the past and pushed it out another `86400` — skipping a full day.

The docstring already claimed "the loop must survive a DST shift". It did not.
Nobody noticed because Moscow has had no DST since 2014; a third of this repo's
200+ forks do.

> **Correction (2026-10-29).** The "200+ forks" figure in this archived
> review was never true -- the repository has 0 forks and 0 stars. The claim
> it carried (other deployments live in DST zones) is the reason the bug
> survived: nobody saw what they could not see.

---

## The class of bug to hunt

Both of the above were the same shape: **something that reports success while
doing nothing, or reports one thing while doing another.** A healthy-looking
log line, a counter that ticked, a tripwire that was green.

Ask of every claim below: *is the thing that moves the same thing that the thing
being moved controls?*

---

## Try to falsify these

Each is a claim made somewhere in the diff. Check them against the code, not
against the commit messages.

### 1. The window cannot slide

`CONTEXT_ANCHOR` has two writers: the module-level assignment and `reanchor()`.
A third path — an import-order side effect, a fork, a reload — would move it.

> `reanchor()` used to be a no-op for exactly this reason (above). The fix writes
> `_DEFAULT.anchor` too. Check the fix is *complete*: does `describe()` and
> `/stats` read the same value the filter does? Is there anything else reading
> `CONTEXT_ANCHOR` directly that would now disagree with the live instance?

### 2. `apply()` is pure

No I/O, no clock, no network, no LLM. Check every method on `ContextBudget`, and
`persistence.load_history`, which is on the request path.

### 3. Nothing deletes history

The old `storage.get_messages()` `DELETE`d rows above 500 at read time. Confirm
it is gone and no replacement trim appeared — including inside migrations or as
a side effect of a `SELECT`.

### 4. `silent=True` really shows nothing

In the group absorb path the LLM runs and reasons, but the user must see only a
reaction. The guard is at the top of `push_thinking`. Find *every* path that
could write reasoning into the chat: streaming SSE, non-streaming, the tool
loop, and the fallback that returns `reasoning_content` when `content` is empty.

### 5. `persist()` runs before `call_llama()`

In the absorb path (`dispatch.py`, `_handle_reply_to_other_user`). If the LLM
call raises first, the message is lost — the exact regression T65.1 fixed.
Verify the ordering is real and that the AST test asserting it is sound.

### 6. The persona cannot leak into forks

`personality.json.example` ships with `enabled=false`; the loader reads only
`persona.local.json`. If someone flips the example to `true`, 200+ deployments
silently acquire an identity. Is there a test that fails?

> **Correction (2026-10-29).** The "200+ deployments" figure in this
> archived review was never true -- the repository has 0 forks and 0 stars.
> The blast radius it described was the right shape; the number was not.

### 7. `update.sh` cannot deploy a partial build

It syncs the whole tree and refuses if files are missing after sync — but the
post-sync check is skipped under `DRY_RUN`. Find any other hole: files that
could go missing, files that could go stale, secrets that could leak into logs.

### 8. Summarisation degrades, never breaks

Any failure in `summarize.py` must leave the bot running on the plain window.

> Note: a *stale* summary is deliberately kept on a failed refresh — the anchor
> only moves forward, so a stale summary can be incomplete but never wrong. If
> you disagree with that, argue it; `test_t70_summarize.py` pins the current
> behaviour deliberately.

---

## Specifics worth a second look

- **`reanchor.py`** — `seconds_until_next` now uses calendar arithmetic.
  Check it for any remaining case the flat-day assumption hides: negative
  deltas, zero deltas, a busy-spin in the nested loop, or behaviour if the
  clock jumps backwards.
- **`summarize.py`** — walks every thread on every re-anchor. On a deployment
  with hundreds of threads that is hundreds of sequential model calls. Is there
  any bound? What is the wall-clock? Does one thread's failure abort the run?
- **`summarize.py` + `apply_summarize`** — the synthetic pair is prepended. Is
  the newest message guaranteed to survive? Can a long summary push the current
  turn out of the engine's budget? Is there a length guard?
- **`call_llama.py`** — the persona is prepended to two prompt branches. Is
  there a third?
- **`storage.list_chats()`** (added for T70.3) — must respect the
  `(chat_id, thread_id)` isolation the rest of the code enforces.

---

## Not in scope

- `bash_command` sandbox, MCP wiring, donsetch tools — unchanged here
- Model quality, prompt wording, personality content
- Engine-side performance tuning

---

## Verdict

**APPROVED — 2026-10-21.** Two review rounds: an architectural pass (Qwen,
`f41dcf9` era) and a verification pass over the fixes.

What the rounds produced:

| Found | Disposition |
|---|---|
| `reanchor()` moved a variable nobody reads | fixed `0b6684d` |
| Daily timer fired an hour late across DST | fixed `4cc6291` |
| `_send_window` leaked a key per chat forever | fixed `9a23083` |
| Dispatch tasks created and dropped | fixed `9a23083` |
| bash rate-limit "default" bucket | did not reproduce — `session_id` is always `chat-{chat_id}` (T57 #S16) |
| WAL not enabled | did not reproduce — already on, with a check that it stuck |

The architectural review was right about the monolith and wrong about two
specific claims. Both are recorded in `TECH-DEBT.md` so the next reviewer does
not re-walk them.

**Sequencing, per the operator:** the monolith refactor happens after this
blocker and its pending tickets close, and before the next feature. That is a
decision on record, not an intention — `TECH-DEBT.md` items 1–4 are the backlog
for it.
