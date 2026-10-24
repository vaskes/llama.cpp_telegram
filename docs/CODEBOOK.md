# CODEBOOK — `vaskes/llama.cpp_telegram`

**Written:** 2026-10-23 by a review agent, against `84de47a`.
**Revised:** 2026-10-23, against `cb6304c` (2110 tests green), after every
claim in it was checked against the tree and four of the mechanical claims
turned out to be wrong. The revision is in §8 — it matters, because the
first version of this document asserted tripwires that did not exist.
**Purpose:** to prevent monoliths from regrowing on their own, and to stop
decomposition from silently injecting new bugs.

This is not a style guide. Ruff already exists. This is about the only failure
mode this repository has ever actually had.

---

## 0. Why this document exists

A week of work broke down the monoliths. In that week, **not one of them grew
back** — and that fact proves nothing about whether they regrow.

They did not grow back because for nine days straight we took them apart by
hand, each one, with a tripwire written for every piece. Nobody opened this
repository and *decided* to grow one. Nobody typed a monolith into existence
by choosing to. Monoliths here emerged from ordinary commits that each added a
branch, and it took nine days to unpick.

**So the question this document answers is not "how do we stop that again."
It is: on the first day after we stop looking, what will make it visible.**

The answer has to be a check that runs without a human remembering to look.
Everything below is either a tripwire that already exists, or is marked
`JUDGEMENT` and must be argued in a commit message.

**The contract, stated once, because the first version broke it:** a rule
marked `AUTO` names a file that exists and a test that runs. A rule marked
`JUDGEMENT` has no mechanical check, and saying so is the whole point of
the label. A rule that is neither is a wish, and a wish in a document
that calls itself a codebook is worse than no document, because the next
reader trusts it.

**What a rule marked `AUTO` is not allowed to be:** a rule that the tree
already violates. A budget that four functions are over is not a budget,
it is a complaint. Either the exceptions are registered, with a date, a
reason and a trigger — or the rule is wrong, and the rule is what gets
changed, not the tree.

---

## 1. What a monolith actually is here

**Not line count.**

Our largest file right now is `handlers.py` at 1365 lines, and it is
**healthy**. It is large because four handlers share one shape. That
repetition is cheap: there is one proven skeleton, and a fix to the skeleton
breaks four call sites *identically*. Nothing can drift.

`_selftest` at one 1940-line function was **not** healthy. It was large because
seven unrelated concerns lived in one body, and a change to any one of them
had to reason about the other six.

### The test for it

> **Name the second, unrelated thing living in the same body as the thing you
> are changing. If you can name it, that is a monolith — regardless of size.**

- `_poll_loop` also runs a reanchor scheduler and a boot summariser →
  **monolith**.
- `call_llama` also opens an SSE connection, folds it into four buffers, and
  carries abort state across iterations → **monolith**.
- `handlers.py` has four handlers that all do the same six things → **not** a
  monolith. It is one pattern, written four times, on purpose.

### The test that actually predicts whether we can work on it

> **Can you write a test that exercises your thing without setting up the
> other thing?**

- `_select_still_running` — pure. Testable.
- `_build_application` — pure. Testable.
- `_register_bot_menu` — built its own `Application`, called
  `app.initialize()`, registered handlers, called `setMyCommands`, and read a
  global dispatcher. **Its only reachable test was an AST check.**

That last one is the whole story of this repository. The monolith was not the
big file. The monolith was the **fact that the code had no seams**, so the
only thing anyone could write was a test that read the source text and hoped.

### Secondary signal, not a rule

> **If a function holds more than about four unrelated things — not branches,
> not lines — it is a monolith regardless of length.**

`run_one` at 319 lines, nine concerns. `handle_photo` at 245 lines, one concern
plus a media path. `call_llama` at 324 lines, four concerns and a documented
`DO NOT REFACTOR` with a trigger. Length is a proxy. Concern count is the
signal. Where they disagree, count the concerns.

---

## 2. The rule that prevents regrowth

> **Every `try:` block that begins `except ... raise` without performing a
> mutation or a `return` is a monolith marker.**

> **Any sequence of lines with no `await`, no `try`, and no early exit that is
> longer than about ten statements is a monolith marker.**

These two are checkable without reading the whole file, and they are the shape
monoliths actually take in async dispatch code. `try:`-wrapping the entire body
so that *nothing* happens in the `except` is what a function looks like on its
way to 300 lines, and it is invisible to every check we have: ruff does not
flag it, tests pass, the import graph is clean.

### Were these measured before anyone wrote a test for them? Yes.

Both markers were run over the production tree before anything was written
down as a rule, because a marker that fires on fifty places is a marker
whose threshold is wrong, and a rule nobody can obey is a rule that gets
ignored.

**Marker A — `try:` whose handler only re-raises.** Two places in the whole
tree: `dispatch_polling.py` (`except asyncio.CancelledError: raise`) and
`handlers.py` (`except BaseException: raise`). **Both are correct code.** A
bare re-raise inside an `except` is how cancellation and rollback are
propagated; the `try` earns its place. A tripwire that fired on those two
would be the "tripwire that reddens on correct code" failure this document
warns about in T-1, and the first person to hit it would delete the real
check with it.

**Marker B — a run of more than ten statements with no `await`, no `try`
and no early exit.** Thirteen places, and the list is a list of *uniform*
sequences: `__init__` in `llm_iteration.py` (18 — field initialisation),
`_attach_handlers` (15 — handler registration), `render_persona` (13),
`_safe_sender_label` (11). Those are not monoliths. The two real ones,
`run_one` (16) and `call_llama` (14), are already over the function limit
and already registered as debt.

So both markers are **not** tripwires. They stay in this document as
`JUDGEMENT` — as things to look for while reading, which is what they are
actually good for — and they are not a check anyone can pass or fail.

### What actually prevents regrowth: the size budget

The mechanical barrier that survives measurement is not a marker, it is a
budget, and it is `tests/test_size_budget.py`:

* a function is at most **200 lines** unless it is registered as an
  exception with a date, a reason and a trigger (S-1);
* a production module over **1000 lines** is in `docs/tech-debt/`, with its
  current size declared (S-6).

Both halves were proved against mutations before being written down: a
227-line unregistered function, a big module removed from the register, a
declared length that lies, and an exception that has outlived its reason —
each one turns the corresponding test red, and the unmodified tree is green.

Budgets beat markers here for a boring reason: a budget has a number, a
number can be exceeded, and an exception to a budget has to be written
down. A marker has a shape, and a shape either fires on correct code or
misses the thing it was aimed at.

**The numbers are not the point. The registration is.** A budget nobody
registers an exception in is a budget that gets raised to fit the tree.

### Not adding a Sonar job, on purpose

SonarQube does have what Ruff does not — `python:S3776` cognitive
complexity, and a per-file function count. It was the obvious candidate
for enforcing the above mechanically, and it was measured against the
alternatives before being declined:

1. **It needs a service and a token.** Every other check in this
   repository runs in a container that already exists. Sonar adds an
   external dependency to CI, and an external dependency in CI is
   something that has to keep working when its vendor has a bad week.
2. **The thresholds would be guesses.** The numbers in the original
   draft of this section were taken from the shapes of our own
   functions, and cognitive complexity is not the same measure as line
   count — the first run would have produced numbers nobody had
   predicted, and setting a gate on them means choosing a threshold
   that is already wrong.
3. **An advisory job nobody reads is worse than no job.** The reasoning
   already written into `lint.yml` — a blocked push costs more than the
   finding — applies with more force to a job that is advisory *and*
   noisy. The failure mode is not that it stays advisory; it is that it
   gets deleted.
4. **The local budget already covers the class.** S-1 and S-6 catch
   growth in the two units that grew here, without a network.

**When to revisit:** if this repository ever takes on an external CI
service for something else, or if the size budget turns out not to catch
what actually grew next time. Both are concrete, and both are cheap to
act on. Until then this is a "not now" with a reason, not a "not ever".

### What happens without it

This is the concrete failure this section exists to stop, from our own history:

`dispatch.py` and `call_llama` were decomposed **correctly** — tripwires were
written, mutation runs passed, the import graph went acyclic. Then three
subsequent commits each added a small feature, and the result was a file where
`main()` was never called by any test. The suite was green at 2048 tests while
the startup path had **zero** coverage.

That is not a hypothetical. It is how `dispatch.py` went from 1672 lines to
~900 with a completely uncovered `main()`.

---

## 3. Rules

Each is marked `AUTO` (a tripwire exists, file named) or `JUDGEMENT` (no
mechanical check; must be argued in the commit message).

### 3.1 Names and scope

**N-1 — `global X` does not create the name.** `AUTO`
`tests/test_module_imports_resolve.py`, the 8-case matrix.

```python
def set_dispatcher(app):
    global _dispatcher
    _dispatcher = app       # does NOT create a module-level binding

def reader():
    global _dispatcher
    return _dispatcher      # NameError unless something above bound it
```

Wave-4 blocker: `_register_bot_menu` moved with `global _dispatcher` into a
module where nothing bound it. The bot would have died at startup with a
`NameError` while registering its command menu. **2047 tests passed.**

The trap is in the checker: an unscoped `ast.walk` picks `_dispatcher` up from
the *function-local* assignment. Any filter built on **union** is powerless —
`out |= (...)` cannot subtract:

```python
out -= declared_global                                    # remove all
out |= {g for g in declared_global if g in module_level} # re-add the real ones
```

**N-2 — `nonlocal` is not `global`.** `AUTO`
Separate set. `nonlocal` names an **enclosing function**, not the module;
subtracting it produces a false positive on correct code. The matrix pins it
as *must not flag*.

**N-3 — A moved function carries its statements and zero imports.** `JUDGEMENT`,
partially `AUTO`

Moving ten handlers into `commands/` gave six modules a `NameError`:
`start_cmd` called `asyncio.to_thread` six times and never imported `asyncio`;
`subtalking` needed `json`; `stats_cmd` needed `fetch_tools_from_llama`.
`py_compile` is silent — each module is individually valid.

`test_no_module_reads_a_name_it_does_not_bind` catches names it can see. It
cannot see an import that is simply absent, because an absent import looks
exactly like a name bound nowhere. **That gap is closed only by running the
thing.**

**N-4 — `monkeypatch` the module where the name is *called*.** `JUDGEMENT`

A test patched `dispatch._reply`. After the move, handlers do their own
`from handlers import _reply`, so the assignment rebinds a name nobody reads.
The real `_reply` ran, got a fake update, raised, and the assert blamed the
handler. **The test stayed green and verified nothing.** In a diff this is
indistinguishable from an ordinary assignment — reading is the only way.

**N-5 — One declaration per constant; every consumer names it.** `AUTO`

`allowed_updates` was written twice: a JSON string in `dispatch_polling`, a real
list in `dispatch_webhook`. `edited_message` could vanish from one side
unnoticed — which is exactly how edit-replace stayed dead in production while a
test reported it working.

The transports **cannot** match on format (`getUpdates` requires a JSON string
in the query; `setWebhook` takes an array in the body). They must match **on
set**.

The check is stronger than it reads: `test_late_import_policy.py` asserts
`isinstance(value, ast.Name) and value.id == const` on the dict value, so
`list(...)`, extra whitespace, an intermediate variable, and a filtering
comprehension all go red. **9 mutations, 9 caught.**

### 3.2 Time and concurrency

**C-1 — `if x is None:` followed by `await` is forbidden.** `AUTO`
`test_concurrent_updates_build_exactly_one_application`.

```python
if app is None:              # everyone read None
    app = _build_application()
    await app.initialize()   # ← the switch point
    set_dispatcher(app)      # everyone still writes their own Application
```

10 gathered updates produced **10 Applications and 20 scheduler runs**. For
`_summarise_on_boot` that is not a leak, it is an OOM: the function itself
promises sequential execution on a single-GPU box.

Correct form is double-checked locking with the whole body inside the lock.
Mutation "remove the re-read inside the lock" → CAUGHT.

**C-2 — `offset` advances only after a successful hand-off.** `AUTO`
`test_offset_advances_only_after_a_successful_hand_off`

`_spawn_dispatch(upd_dict)` without the keyword-only `dispatch_update=` raised
`TypeError` on every update. `offset` had **already** moved, so Telegram
counted each message as delivered. The bot ran, logged at INFO, and answered
nothing. Logging at `log.info` here is itself part of the bug.

**C-3 — A fix that changes the *timing* of something needs a test for "before".** `AUTO`

`_install_signal_handlers` ended with an unconditional `SHUTDOWN_EVENT.set()` —
not inside the signal handler, but in the function that *installs* handlers.
`llm_iteration` reads exactly that object and handlers do not pass
`shutdown_event`, so **every tool-loop iteration aborted at iteration zero**.

| | original C2 | the "fix" |
|---|---|---|
| guard | never fired | **always** fired |
| symptom | bot silent | `[bot: shutdown requested]` |
| polling | broken | broken |

The test baked the bug in as expected behaviour:
`assert ev.is_set(), "precondition: the signal handler set the event"`.

**Installing a handler and receiving a signal are different events. Only the
second may set the flag.**

**C-4 — A module never rebinds state it does not own.** `JUDGEMENT`
Events, counters and semaphores live in **one** owner module. Everyone else
receives a reference by parameter or reads the attribute. Rebinding another
module's name through `global`, `from X import Y`, or `monkeypatch` is
forbidden. This rule is what C2 was, and the "fix" for C2 was the same rule
broken again.

**C-5 — `except BaseException`, never `except Exception`, in cleanup paths.**
`JUDGEMENT`
`CancelledError` inherits from the former. It leaks identically and **never
reaches the log**.

**C-6 — A slot is acquired before the `try` that releases it.** `AUTO`

```python
sem = await _check_user_slot(...)       # slot taken
if sem is None: return
await update.message.chat.send_action()  # ← outside try: an exception here
try: ...                                #    destroys the slot permanently
finally: sem.release()                  # never reached
```

At `_PER_USER_SEMAPHORE_LIMIT=2`, two failures lock the user out until
restart. Verify with `sem._value` after injecting an exception — an AST check
for this class lied twice.

### 3.3 Size

**S-1 — A function is ≤ 200 lines unless registered as an exception.** `AUTO`
`tests/test_size_budget.py`. The exceptions live in the test file, next to
the check that reads them, and each one carries a date, a reason and a
trigger — the same three things a debt item in `docs/tech-debt/` carries.

There are four, and it is worth reading what they are:

| Function | Lines | What it is |
|---|---|---|
| `call_llama` | 324 | Three unrelated concerns. `DO NOT REFACTOR`, debt item 03. |
| `run_one` | 319 | One tool-loop iteration, nine concerns. Debt item 08. |
| `handle_photo` | 245 | One concern plus a media path. Debt item 13. |
| `handle_text` | 209 | Not debt. Same shape as `handle_photo`. |

The fourth one is the reason this rule exists. `handle_text` crossed 200
lines at some point during nine days of refactoring, and nobody noticed,
because there is no line count anywhere in this repository that says "this
function is too big now" — only a number in a document, written by hand,
about a different unit. It is registered as an exception, and being
registered is the point: the next person to grow it sees the entry.

**A rule the tree already violates is not a rule.** Before S-1 was written
down, the tree violated it in four places. That is not a reason to lower
the limit — it is a reason to write the four exceptions down, and to
check the tree before writing the rule at all.

**S-2 — A module over 500 lines carries a section map.** `JUDGEMENT`
*Not mechanically checked, and the original claim that it was is what §8
is about.* `test_section_markers.py` exists and is real, but it looks at
**`handlers.py` only**, and what it enforces is narrower than "a module
carries a map": that between a marker and the function it names there is
no *other* definition, that every top-level function in that one file is
either marked or on an allowlist with a stated reason, and that a marker
naming a renamed function goes red.

The 500-line threshold itself is enforced by nothing. Three of the five
production modules over 500 lines have a section map; `storage.py` has
one marker for 940 lines, `bash_command.py` has two for 590, and
`llm_iteration.py` has none for 507. Writing this rule down as `AUTO`
would have been a claim with no check behind it — see D-1.

**S-3 — One concept, one owning module.** `AUTO`
`call_llama` talks to the engine and nothing else. Transport lives in
`dispatch_*`. State lives in `state.py`. Limits live in `limits.py`.
Violating this produced C2.

**S-4 — A move is not an extraction; it is an extraction plus a covered seam.**
See §2 and rule T-8.

**S-5 — Between a section marker and the function it names, nothing else is
defined.** `AUTO`
`test_section_markers.py`. Not "immediately" — distance is a convention and
conventions drift; the property is that a reader following a marker lands
on the function it introduces and not inside a different one. A marker
inside another function is red.

**S-6 — A production module over 1000 lines is in the debt register, with
its current size declared.** `AUTO`
`tests/test_size_budget.py`, against the `<!-- current: file=N -->`
declarations that `test_tech_debt_doc_sizes.py` already keeps honest.

`handlers.py` is 1365 and is registered. The threshold is 1000 because
that is above every module that is not in the register and below the one
that is — a limit set by the tree, and the tree is allowed to be above it
only through a written exception. The register is the place where "this
file is big and here is why" lives; a module that grows past the limit
without an entry there is exactly how the register stops describing the
debt.

### 3.4 Comments and docstrings

**D-1 — A comment making a checkable claim must be falsifiable by a mutation.**
`AUTO` (for the execution part)

`assert "LLAMABOT_SELFTEST" in source` — the string appears five times, the
guard once. The mutation "remove the guard" survived. And the price was not
cosmetic: `dispatch.py` calls `_selftest()` unconditionally, `main()` catches
only `KeyboardInterrupt`, so without the guard `InvalidToken` escapes
`asyncio.run` and **the bot does not start at all**.

> If a comment says "the guard is here", either code enforces it or the
> wording must be careful enough not to lie at the first refactor.

**D-2 — Line-number references go stale.** `AUTO`
Four comments (`handlers.py:673`, `call_llama.py:1401`, …) were stale, and one
went stale **from the very commit that verified it**.

Cite **meaning**, not a line: not `handlers.py:673`, but "in
`_dispatch_update`, before `Update.de_json`". If a number is genuinely needed,
a test that fails when the line moves must sit next to it.

**D-3 — A comment longer than its code is usually correct here.** —
`dispatch_polling.py` carries twelve lines explaining why a backoff that grows
on two consecutive failures produces a 55-second delay to answer, on a bot that
is working perfectly. That ratio is right. **Do not compress the explanation
for why check-then-act across `await` is forbidden.** That trade is not in our
favour.

**D-4 — A name in a ticket is a claim. Check the function's history.** `JUDGEMENT`

A ticket prescribed moving `_safe_sender_label` into `sanitizer.py` and warned
that "sanitisation here is display-name normalisation, not a security filter —
do not let a future reader add filtering". The function **is** a prompt-injection
filter and always was (`git show 4eedfa7` added `unicodedata` and the
`system|assistant|user|...` regex alongside the function).

Following the ticket literally would have caused the harm it warned about,
just in the other direction.

### 3.5 Tests

**T-1 — A tripwire you cannot make redden is worse than no tripwire.** `AUTO`

Mutating your own tripwire costs less than writing it: remove the protection
you are claiming to pin, and confirm red. Two minutes.

**The converse is equally true: a tripwire that reddens on harmless
refactoring is also worse than no tripwire.** It teaches people to hate it, and
it gets deleted "automatically" — taking the real check with it. This happened
here: a blanket ban on `rollback('aborted')` was wrong (that reason means "the
turn did not happen", which occurs on both a `call_llama` error and a failed
persist), and a blanket ban on the "stopped" string outside `llm_abort` was
wrong (`dispatch.py` writes it legitimately on the callback path).

Phrase the check as **a property inside its own scope**, not a global ban.

**T-2 — `isinstance(node, ast.Constant)` for an identifier is dead.** `AUTO`
`dict` is a builtin, so it is `ast.Name`, never `ast.Constant`. A 15-line check
passed against a handler that had the inline copy put back: **18/18 passed**.

And the same shape: a tripwire answering "is there one" where the question is
"how many" is dead identically. Check `== 1`, not `in calls`.

**T-3 — Green alone, red in the suite.** `JUDGEMENT`
Environment variables only in a subprocess. `config` reads env at import time, so
an in-process `monkeypatch.setenv` on an already-imported module silently does
nothing, and the block runs against a foreign `CONVERSATIONS_DB`.

**T-4 — A duplicate definition in Python is not a compile error.** `AUTO`
`test_blockers.py` once held **19 copies** of one 60-line test. Python binds the
last one; the other eighteen never executed — 1115 lines of dead code, invisible
to a green suite and to a passing mutation run.
`test_no_duplicate_definitions.py` catches any repeated top-level definition.
It found a live one immediately: `Path` imported on line 25 and again on 199.

**T-5 — A golden signal is a sequence, not a set.** `AUTO`
A missing block produces no line; a block that started lying produces a
different one; two blocks that swapped places produce **the same set in a
different order**. A set cannot tell the last case.

Check the marker count **separately** from deduplication, or the dedup hides a
block that started running twice.

**T-6 — "Was the argument passed" ≠ "was the right value passed".** `AUTO`
`register_bot_menu=None` — argument passed, check satisfied. Three such
mutations stayed green across the whole suite.

**T-7 — A test pinned to a file that moved converts to a symbol.** `AUTO`
Not "the new file somewhere" — that makes the check either dead or about the
wrong thing. `tests/dispatch_location.py` is the model: a list of modules
rather than a glob, and `source_of` returns `""` instead of raising.

**T-8 — Extraction requires a surface that could not have existed before.** `JUDGEMENT`

A unit test of the extracted function does not count — it calls the function
directly and is green for any shape of the seam.

> When `ToolLoop.run_one()` was extracted, 13 mutations were caught and **one
> survived**: removing `.run()` from `call_llama`'s return. `call_llama`
> returned a `ToolLoop` instead of an answer, and **333 tests stayed green**,
> because all 17 tests of the new unit called `run()` directly. None of them
> could see whether `call_llama` called it at all.

**Two directions are mandatory:** the unit itself, and the caller with a
stubbed engine asserting on the value that comes out.

**T-9 — Comments describing a location must survive their own refactor.** `AUTO`
Covered by D-2. The edit-replace block was described in two comments as living
"in `_dispatch_update`" for years while it lived nowhere at all.

### 3.6 Delivery and diagnosis

**X-1 — A feature that lives inside one transport is dead in the other.**
`JUDGEMENT`

This bot runs two ways: long polling, and a webhook. They are not two
implementations of the same thing — they are two different paths through
the code, and a block placed in one of them does not run in the other.

The edit-replace feature lived inside `for upd_dict in updates_raw:` in
the polling loop. Production runs in webhook mode, where the update
arrives straight from the webhook handler and never enters the polling
loop. **The feature had never executed in production.** The test that
covered it passed, because the test called the polling path directly.

> **"The feature is in the code" is not "the feature runs in the mode
> production is in."** Find where the block physically is, not which file
> it is described in.

The rule that catches it: a feature that both transports must have lives
on the **shared** path — the one that parses an `Update` and hands it on.
Anything transport-specific is allowed to be transport-specific, and
nothing else is.

**X-2 — A diagnostic string must read a variable that was assigned in
the same scope.** `JUDGEMENT`

`body=''` was printed by `r.text[:500] if r is not None else ""`, where
`r` was a variable of the *calling* function, never assigned in the one
that printed it. The log said "the body was empty" and "the body was
unknown" in exactly the same words.

That cost half an hour of chasing the wrong end: the server was not
returning empty bodies, the code was not choosing a branch, and the
`use_stream` flag was not being passed. None of it was visible in the
log, because the log could not distinguish "empty" from "unknown".

> **Log the fact while the answer is still in your hands.** Print the
> status, the length, the content-type and the first few hundred bytes
> of the body *at the point of parsing*, before the variable that holds
> it has gone out of scope.

A log line that lies about what it knows is worse than no log line: it
is a false lead, and false leads cost more than silence.

---

## 4. Rules for the reviewer and the harness

These are the ones that cost me personally, three waves running. They are about
how a review is *run*, and they explain why this repository looks as healthy as
it does.

**H-1 — `n_sub == n_expected` before substitution, otherwise `REFUSED`.** `AUTO`
`.replace(a, b, 1)` without an assert is a silent no-op. **Twice** I reported
"survived" for a tree I had never actually modified.

**H-2 — `passed == prefix && failed == 0` is `REFUSED`, not `SURVIVED`.** `AUTO`
Matching the prefix count with zero failures is the strongest available signal
that nothing changed, and it is readable in one second. I missed it, twice,
with the rule already written down in my own notes.

**H-3 — A mutation proving a fix must have the shape the fix repairs.** `AUTO`

> I fixed the `global` filter and verified with "a function declaring `global`
> reads an unbound name". That is caught by the **unfixed** checker too — the
> name is bound nowhere. I proved the tripwire was alive, not that the fix did
> anything.

**Negative evidence is mandatory: reverting the fix to the old formula must
produce `SURVIVED`.** If the revert produces `CAUGHT`, the test is detecting
something else and the fix is unverified.

**H-4 — A mutation must go red in finite time.** `AUTO`
Removing the N7 startup guard made `webhook.run()` **hang** — it bound the
socket and waited on the shutdown event forever. The suite hung, which reads as
infrastructure trouble rather than as a caught regression. Wrap in
`asyncio.wait_for(..., 5)` + `pytest.fail` → `Failed in 6.22s`.

**H-5 — A mutation run against a subset says nothing about another subset.**
`JUDGEMENT`

> A mutation run against `test_dispatch_edited_message.py` says nothing about
> `test_late_import_policy.py`. The green belonged to a different run.

**A review wave starts with a full pass over the tree. The diff is the second
step, for locating things — not the first.** Reviewing three commits wrote
"no blockers" while blockers sat in code from an earlier wave.

**H-6 — `py_compile` validates syntax. `ast.parse` does not.** `AUTO`

```
compile(): rejects -> 'await' outside async function
ast.parse(): ACCEPTS
```

Every refactor script I wrote that validated through `ast.parse` was blind to
this class. It would have written a module that cannot be imported, and the bot
would have died at startup with a `SyntaxError` while `ast.parse` in the script
was the "check".

Same for f-strings: `ast.parse(..., feature_version=(3, 11))` **does not** catch
PEP 701. And a detector looking for `STRING` tokens prefixed `f` fires exactly
on the version that *accepts* the construct — the inversion of the task.

**H-7 — Inserting a multi-line string as one list element desynchronises
everything.** `JUDGEMENT`

```python
lines[i:i] = [HELPER]                                  # list +1, text +40
lines[i:i] = HELPER.splitlines(keepends=True)         # correct
```

Every span after the insertion points at the wrong line, and the failure reads
as "expected 'except' or 'finally' block" — Python's fault, not the script's.

Assert on the edges of the span before replacing:

```python
assert first_line.strip().startswith("result = await _route_to_thread")
assert "return" in last_line
```

**H-8 — `lines[a:b]` slices lines; `src[a:b]` slices characters.** `AUTO`

A test that took `body = src[lineno-1:end_lineno]` received **the start of the
file** and was satisfied: its assertion about `callback_query` was checking a
comment in the header.

**H-9 — Regex needs a word boundary.** `JUDGEMENT`
`webhook\.py` matches inside `dispatch_webhook.py`, and a size-recording regex
quietly rewrote `dispatch_webhook.py=327` as `webhook.py=208`. `(?<![A-Za-z0-9_])`
cures it. **Check the result against the actual numbers, always.** This class
has cost time three times.

**H-10 — Restore a mutated file with `git checkout`, nothing else.** `AUTO`

`git stash push -u` does **not** hide untracked files: the new modules stayed
put, and my next step deleted them along with the only copies. A backup taken
*after* the mutation contains the mutated file. `git status --porcelain` after
every step is not optional.

**H-11 — `git commit --amend` only after the commit is pushed.** `JUDGEMENT`
`--amend` rewrote the wrong commit: a message about `handlers.py`, the contents
of `state.py`. Before a push it rewrites history you have already mentally
closed.

**H-12 — A green suite is a claim about the suite.** `JUDGEMENT`
Every mutation caught in this repository was caught by a tripwire, and every bug
found was found by running the thing or by a mutation. `2074 passed` coexisted
with a bot that lost every message.

**H-13 — A comment inside a `\`-continued line is not a comment.**
`JUDGEMENT`

The lint job was red for a hundred consecutive runs and had never once
passed. The cause was three lines of YAML:

```yaml
pytest tests/ \
    # this one still needs a live side-car
    --ignore=tests/test_shell_integration.py \
```

A backslash binds only the next line — and the next line was the comment.
So the shell ran `pytest tests/`, then tried to execute
`--ignore=tests/test_shell_integration.py` as a command:

```
line 13: --ignore=tests/test_shell_integration.py: No such file or directory
##[error]Process completed with exit code 127.
```

**The tests passed.** The number `2029 passed` was printed a line above the
exit code 127, and nothing in the summary said the job had failed for a
reason unrelated to the tests. A hundred red runs, all of them successful
runs marked as failures.

The cheap check: extract the `run:` block and execute it with a real
`sh` and a stubbed `pytest`. The old form gives `ARGS: tests/` and exit
127; the new form gives every argument in one command. Five seconds, and
it catches what reading the YAML does not.

---

## 5. Adding a rule

1. One falsifiable sentence.
2. The tripwire — file name and test name.
3. **Mutate the tripwire and confirm it red.** Without this, step 3 is not done.
4. For a fix: reverting it must give `SURVIVED`.
5. Exceptions go in `docs/tech-debt/` with a date, a reason and a trigger.
6. **Check the tree before writing the rule down.** If the rule is already
   violated, then either it is wrong, or the violations have to be
   registered before the rule can be called a rule. This step is last
   because it is the one that gets skipped — it is the one where the
   answer is "the rule as written would be red today", and the easy way
   out is to mark the rule `AUTO` and move on.

Step 6 is not a formality. The first version of this document marked four
rules `AUTO`, and all four were wrong — see §8.

The register's own rule, already in `TECH-DEBT.md`:

> *If you find yourself adding a third entry without removing one, the debt is
> growing faster than it is being paid and that is its own finding — escalate
> it.*

It would have fired if items 1, 3 and 5 had been closed when they were done.

---

## 6. Existing tripwires

| File | Tests | Protects |
|---|---|---|
| `test_import_graph.py` | 15 | Import cycles, Tarjan |
| `test_refactor_graph.py` | 12 | Graph after the split, late imports |
| `test_tech_debt_ledger.py` | 11 | The debt register |
| `test_late_import_policy.py` | 7 | `allowed_updates` in both transports |
| `test_section_markers.py` | 6 | Markers not inside functions, not orphaned |
| `test_module_imports_resolve.py` | 5 | Names + the `global`/`nonlocal` matrix |
| `test_tech_debt_index.py` | 5 | Register links point at real files |
| `test_drift_call_llama_names.py` | 4 | Name drift in extracted modules |
| `test_drift_t58_revert.py` | 4 | T58 regressions |
| `test_tech_debt_doc_sizes.py` | 3 | `<!-- current: file=N -->` vs the tree |
| `test_drift_m5_dedup.py` | 3 | Single source for `BASH_COMMAND_TOOL_DEF` |
| `test_drift_t63_env_names.py` | 3 | Env-var inventory |
| `test_drift_r3_chat_type.py` | 2 | `chat_type` keyword-only and required |
| `test_drift_s16_session_id.py` | 2 | One `sid`, no `args.get("session_id")` |
| `test_drift_handler_imports.py` | 2 | Application callbacks imported |
| `test_drift_r8/r9/r10/r11` | 4 | Tools, profiles, env, handlers |
| `test_polling_integration.py` | 9 | Cross-transport integration |
| `test_edit_replace_live_path.py` | 7 | The live edit-replace path, in-process |
| `test_size_budget.py` | 10 | S-1, S-6 — function and module size budgets |
| `test_changelog_structure.py` | 7 | One `[Unreleased]` section; sections and tags agree |
| `test_no_duplicate_definitions.py` | 4 | T-4 — repeated top-level definitions |
| `test_no_model_coauthor_trailers.py` | 4 | No false attribution in the history |
| `test_dispatch_group_seam.py` | 4 | The seam a group-absorb extraction needs |
| `test_signal_handler_ownership.py` | 3 | One signal policy, two modes |
| `test_c3_initialize_failure.py` | 2 | The publish-after-initialize ordering |

133 test files in total; the table above is the ones that guard a rule in
this document, not all of them.

**One known deliberate bypass:** the ruff CI job is advisory and does not gate
pushes, and the reason is written into the workflow file itself. Do not quietly
strengthen or quietly remove it.

**One known gap:** the live edit-replace path has never been exercised by a
human end to end. `test_edit_replace_live_path.py` is in-process
characterisation, not live coverage, and the README says so too.

---

## 7. Deliberately not in this document

**Formatting rules.** Ruff exists. Its findings are cosmetic and a blocked push
costs more than a `W605` — a decision already made and recorded.

**Type hints.** The repository has almost none. Adding them now is cosmetics
with no observed incident behind it.

**"Files must be under N lines."** Data is against it: `handlers.py` is 1365 and
healthy, because it is repetition of one checked shape, not a mixture.

**"Write tests for everything."** What works is §2 and T-8 — *extraction requires
a new surface*, *a tripwire must be able to redden*. "Write tests" has never
once caught anything here.

**A static complexity analyser (SonarQube).** It does have the one metric
Ruff lacks, and it was measured against the alternatives before being
declined — the reasoning, the four reasons and the condition for revisiting
it are in §2. The short version: it needs a service and a token, the
thresholds would be guesses, an advisory job nobody reads gets deleted, and
the local size budget already catches what actually grew here.

---

## 8. What was wrong with the first draft of this document

This section is not history for its own sake. The first version of this
codebook was written by a review agent, and every mechanical claim in it
was checked against the tree before being committed. **Four of them were
false**, and the pattern is worth more than the four fixes.

| First draft said | Reality |
|---|---|
| S-1: a function is ≤ 200 lines. `AUTO` | Nothing checked function length. `test_tech_debt_doc_sizes.py` checks the size of *files*. Four functions were over the limit, including `handle_text` at 209, which had crossed it unnoticed. |
| S-2: a module over 500 lines carries a section map. `AUTO` | `test_section_markers.py` looks at `handlers.py` only, and enforces a different property. The 500-line threshold was checked by nothing. |
| S-5: definition, then its marker, immediately. `AUTO` | The test enforces "no *other* definition between the marker and the function" — it says in its own docstring that distance is a convention. |
| §2: "Enforcement: `AUTO` — add `test_no_monolith_markers.py`" and "Also `AUTO` — a sonar rule in CI" | Neither file nor job existed. §0 had promised that everything is either a real tripwire or marked `JUDGEMENT`. |

And the two proposed markers in §2, measured before anyone wrote a test for
them, turned out to be unusable as tripwires: one fires on two places,
both of them correct code; the other fires on thirteen, most of them
uniform sequences. Both are kept here as `JUDGEMENT` — as things to notice
while reading — and the mechanical barrier that replaced them is a budget
with a written exception list.

**The pattern.** A rule written by someone who has not measured the tree is
a wish with a label on it. The label is what makes it dangerous: `AUTO`
means "trust me, this is enforced", and the reader stops checking. It took
a second pass over the whole document, with the tree in front of it, to
find four claims that were not true. The same document, in the same
session, had also written the rule that explains why:
*tripwire, который нельзя заставить покраснеть, хуже отсутствия
tripwire*.

**So the procedure this document now demands of itself:** before a rule
with a mechanical claim is written down, the claim is measured against the
tree, and the tripwire is made to go red by taking the protection away.
If it cannot go red, it is `JUDGEMENT`, and it says so.

**And a second thing, found while fixing the first.** While checking the
claims above, the check turned up a live defect nobody had been looking
for: `CHANGELOG.md` had two sections headed `## [Unreleased]`, the live one
at the top and one written on 2026-10-10 that kept the name through
fourteen releases. Both read as current; the older one was longer.

The debt register has a tripwire. The tech-debt documents have one. The
sizes have one. The changelog — the document a user actually reads when
deciding what to run — had none, and had been carrying a second live
section since before most of these rules were written.

That is the shape of the whole problem this document is about: the
monoliths did not grow because someone decided to write them. They grew
because each commit added a branch, and nothing was watching. The same
is true of an ambiguous changelog, and the fix for both is the same
thing: **a check that runs without a human remembering to look.**

**H-14 — Two sections with the same name means the reader cannot tell
which one is current.** `AUTO`
`tests/test_changelog_structure.py`.

`CHANGELOG.md` carried two `## [Unreleased]` sections for fourteen
releases: the live one at the top, and one written on 2026-10-10 that
kept the name while everything moved under it. Both were complete, both
read as current, and the older one was the longer of the two.

Nothing caught it, because nothing looked. The debt register has a
tripwire, the tech-debt documents have one, the sizes have one — and the
document a user actually reads when deciding what to run had none.

> **A document is not a deliverable until something checks it.** The
> tripwire has to exist for the case where the document is *ambiguous*,
> not only where it is *wrong*: two sections of the same name are both
> correct, and only the count catches them.

The test also checks the other direction of the same drift — a release
section with no tag, and a tag with no section. The two mismatches that
exist today are listed in the test with the reason, so that a new one
is a red test rather than a debt nobody is tracking.

**A tripwire that skips in CI is not a tripwire.** The first version of
this test read the tag list out of the checkout. `actions/checkout`
fetches a depth-1 history and **no tags**, so in CI the list was empty,
both tag checks skipped, and the test was green while checking nothing
— the failure mode this document warns about in T-1, wearing a green
tick.

The fix is to ask the remote as well: `git ls-remote --tags origin`
works in CI, where the checkout has already been authenticated. Both
paths are exercised here: a checkout with tags, and a depth-1 clone
without them, with the remote URL as origin. The second one is the
shape CI has, and it is the one that matters.

### 8.1 The history is part of the deliverable

**R-1 — A co-author trailer is a contribution, and GitHub counts it.**
`AUTO`
`tests/test_no_model_coauthor_trailers.py`.

Twenty-one commits carried
`Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
The model had written no line of this repository, and it appeared on the
contributors page anyway. GitHub reads a co-author trailer as a
contribution.

`.mailmap` cannot fix it. The file is applied to the **author** field —
by git and by GitHub — and a `Co-Authored-By:` trailer is parsed as
written. Removing the trailers needed a history rewrite, which moved every
SHA from `975ab03` onwards, including the release.

The tripwire reads commit messages, but **only the last paragraph**: by
git's own convention a trailer is a `Key: value` line there, and a quote
of a trailer in the body of a message is not one. The first version of
the check searched the whole message and fired on this repository's own
CHANGELOG, which quotes the trailer while describing the incident — a
check that fires on the document explaining the problem is a check the
next person disables when they write the next document about it.

**R-2 — `git worktree` keeps the old history alive.** `JUDGEMENT`

After the rewrite, `git log --all` still showed nine pre-rewrite commits.
`refs/original/*` had been deleted; four linked worktrees in `/tmp` were
holding the objects, because `git gc` does not collect anything reachable
from a worktree. Two of them had uncommitted work in them, including a
mutation and an untracked test file.

> **Before any history rewrite, look at `git worktree list` — not only
> `git branch`.** And keep the old history in a bundle first, so the
> rewrite is reversible.

---

## One paragraph, if you remember nothing else

> **A monolith is not a big file. A monolith is several unrelated things living
> in one body, where a change to any one of them has to reason about the rest —
> and where the only available test is a test that reads the source text and
> hopes.**
>
> Every check in this document exists to notice that. None of them is ruff.
> What actually catches a monolith is running the real path, and a mutation
> that breaks it.
>
> The one thing that is a number is a budget, and it is a number for a
> particular reason: it is the cheapest way to notice that something grew
> while nobody was looking. A budget does not decide what a monolith is —
> `handlers.py` is 1365 lines and healthy — it only decides when somebody
> has to write down why. That is where the judgement lives: in the
> exception, with a date, a reason and a trigger.