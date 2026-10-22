---
tech_debt: 9
marker: tests/test_selftest_runtime.py, tests/test_selftest_blocks.py,
  tests/test_import_graph.py, tests/test_tech_debt_index.py
marker_kind: tripwire   # the marked tests now pin a closed fix
state: closed
issue: null
---

## 9. `selftest._selftest` is a 1940-line function — **CLOSED 2026-10-22**

**Measured 2026-10-22 (when filed):** `telegram-bot/selftest.py` was 2075
lines; `_selftest` spanned 135–2074, so **1940 of them**. That is 2.4x
`call_llama` (825), which was item 3 and drew months of review attention.

**Measured 2026-10-23 (when closed):** `_selftest` is **111 lines**.
`telegram-bot/selftest.py` is 239. The work went into seven leaf modules,
each an `async def check_*() -> bool` with no arguments:

| module | lines | what it owns |
|---|---|---|
| `selftest_storage.py` | 220 | known_topics round-trip, T67 no-prune, the v3 rating column |
| `selftest_handlers.py` | 219 | three handlers that each shipped a NameError |
| `selftest_access.py` | 202 | `_should_mute_in_group`, `_is_reply_to_other_user` |
| `selftest_state.py` | 277 | per-user semaphore, rating grammar, emoji map, chat_member |
| `selftest_media.py` | 344 | `_bot_replies`, download size cap, sender-name flow |
| `selftest_llm.py` | 757 | vision, routing, abort, tool-call JSON, `react_to_message`, GROUP_CONTEXT |
| `selftest_infra.py` | 424 | donsetch, abort_event key, F4 LRU, post-won race, webhook boot |

`run_all()` is a four-line driver per module, and `_selftest` is the
preamble, the `/start` smoke, a list of `run_all()` calls and two log
lines — which is the shape the item was filed against in the first place.

### The harness came first, because there was nothing to regress against

`test_in_tree_selftest` really does call `_selftest()`, but it needs a
live Telegram token and a live llama-server, so it has never passed and
cannot pass in CI -- it dies on `getMe` with the fake token the rest of
the suite uses. The other twelve tests that mention selftest read its
source and assert on the AST.

So this was 1940 lines that compile, that no test executes, and that
therefore have no behaviour to regress against. That is exactly the
position the `call_llama` blocks were cut from, and the one where the
first slice removed the SSE `[DONE]` guard, three guards in total, with
805 tests green.

`tests/selftest_harness.py` stands up local stub servers on all three
endpoints the bot talks to -- `TELEGRAM_API`, `LLAMA_URL`,
`DONSETCH_URL`, all env-driven -- and runs the real function in a
subprocess. No mocking below httpx: half of what these blocks check is
the wire format, and a mocked transport would skip it. Subprocess
rather than in-process because `config` reads its environment at import
time, so the endpoints have to exist before the first import.

`tests/selftest_golden.log` pins the **sequence** of markers, not the
set. A block that stops running emits no marker, a block that starts
lying emits a different one, and a block that swaps places emits the same
set in a different order. All three are what cutting a 1940-line
function actually produces, and none is visible to a structural test.

The golden moved **six lines** across seven slices: one false label
corrected and five module summaries added. No `[OK]`/`[FAIL]` marker
moved, disappeared, or changed at any point. That is the evidence that
each cut landed where it was supposed to.

### Four defects the extraction found, none of which a structural test could see

**1. A summary that was still false after slice 1.** `check_rating_roundtrip`
ended with `[selftest] group-mode tests: all pass`. Slice 1 added a
correct `storage tests` summary *beside* it and left the wrong line in
place, so the golden was pinning a lie. It is now
`[selftest] storage rating round-trip`, and the golden moved by one line
-- a replacement, not an addition.
**2. An order dependency in a block that passed for two reasons.** The
non-streaming `tool_calls` block patches `call_llama.httpx.AsyncClient`
with a fake that has `post` and nothing else. It only ever passed
because the vision block ran first and left the model discovered --
`call_llama` calls `_ensure_model()` on every invocation, which issues
`GET /v1/models`, and the fake had no `get`. Run on its own the block
returned False with
`AttributeError: '_FakeAsyncClient' object has no attribute 'get'`, and
the golden was perfectly happy, because the markers came out the same
either way.

That is `test_every_block_passes_when_run_alone` doing its job, and it
is why the fix went into the fake rather than into the test.

**3. A block with two sections and one verdict line.**
`check_group_context` carries the F2/NEW-2 global LLM semaphore *and* the
GROUP_CONTEXT injection. The cut converted only the second `all_ok &=`
into the return value, leaving the first assigning to a name that had
never been bound inside the new function: `UnboundLocalError` at the
first `all_ok &= sem_ok`, and a selftest that died half way through with
the process exiting non-zero. Nothing structural resolves a function
local, and the golden only noticed because the run stopped.

**4. The aggregate flag was never read, and had never been read.**
`_selftest` wrote `all_ok` twelve times and read it zero times. That was
true before T1.1 as well: the smoke test has no exit code and reports by
logging, so the flag was always write-only.

What made it dangerous was not that it was unread but that it looked
load-bearing. That is precisely how the storage summary came to read the
aggregate of everything before it and label itself `[selftest]
group-mode tests`. A variable nobody reads is a comment; a variable
everyone reads as a running verdict is a claim.

It is gone rather than given a reader. Every leaf now returns its own
verdict *and* logs its own summary line, and the golden pins those
lines, so an aggregate would restate what is already held.
`test_every_block_is_awaited_by_selftest` replaces the direction the flag
used to provide.

Two mutations found this by surviving, and the reasoning is worth more
than the mutations: "discard one block's verdict" is vacuous on a clean
tree because every block returns True -- so it survived, correctly, and
asking *why* it was vacuous is what exposed that there was no aggregate
to wire in the first place.

### The tests went in both directions

A suite that only exercises the new unit cannot see the seam the unit
was extracted to fix -- removing `.run()` from `call_llama` returned a
`ToolLoop` instead of an answer and left 333 tests green, because every
test of `ToolLoop` drove it directly. So each leaf gets:

* a shape check -- module-level coroutine, zero arguments, `-> bool`;
* an isolated run -- each block in a fresh subprocess with its own
  database, which is what found defect 2;
* a caller-side check -- `run_all` reports its own `SUMMARY_NAME` rather
  than a hand-built label, which is what makes defect 1 impossible to
  reintroduce quietly.

`selftest_llm` is driven one block at a time rather than through
`run_all`, because the `selftest_handlers` and `selftest_storage`
summaries sit between its first three blocks and the golden pins the
sequence. Folding them into one call would reorder the golden. For that
module the caller-side direction is `test_every_block_is_awaited_by_selftest`,
which reads `_selftest` and fails on a `check_*` nothing awaits -- the
same seam, checked from the other side.

### Mutation harness

`scripts/mutation_harness_selftest.py`: 14 mutations, all caught. It asks
a different question from the other harnesses in the repo. They ask
whether a guard was removed; this one asks whether a block still runs
and now reports something different, which is the failure an extraction
actually produces and which the golden cannot see, because the marker
lines are unchanged and only their text moved.

Six of the fourteen were wrong on the first run, and the harness is more
useful for what it did with them than for what it scored:

* `mute_ok &= ok` -> `|=` **survived**, correctly. Every case passes on a
  clean tree, so the two agree and no target can tell them apart. A
  mutation equivalent to the code on the only tree anybody runs measures
  nothing; calling that a weak test would be the wrong lesson. Both
  accumulation mutations now invert a case as well, so they ask the
  question the first version could not: when a case really fails, does
  the verdict still report it?
* `return all_ok` -> `return True` **survived**, for the same reason,
  and was rewritten the same way.
* two mutations about the `all_ok` aggregate **survived**, which is what
  found defect 4. They are deleted rather than re-aimed; there is no
  aggregate to aim at.
* "remove the donsetch env guard" **survived**, and is recorded as
  unanswerable rather than counted. The harness runs with `DONSETCH_URL`
  pointed at a stub, so the guard's early return never fires. It is
  reachable in a different environment -- the isolated block test drives
  this block with neither variable set -- but the two environments do
  not produce the same golden, so they are not interchangeable.
  A mutation that cannot be made to red is worse than no mutation: it
  reads as coverage.
* the `return all_ok` anchor expected 1 site and found 2, because the
  module has one per `check_*` plus one in `run_all`. REFUSED, not
  scored.
* the infra verdict anchor assumed the assignment and the return were
  adjacent; the builder puts a summary line between them, so it found 0
  sites. REFUSED. Expect-before-substitution caught a stale anchor
  rather than crediting it as a clean deletion.

Final run: **11 mutations, 11 caught, 0 survived, 0 refused, 0 invalid.**

Preflight runs the target on the clean tree first, so CAUGHT means the
target saw the mutation rather than the target being broken. That is not
ceremony: two of the early runs here reported CAUGHT on a target that
was itself broken, and every preflight in this repo exists because of it.

### One import cycle grew, and it is the old one

`selftest_state` inherits the `{dispatch, selftest, webhook}` cycle:
`check_chat_member` calls `_handle_chat_member_update`, which dispatch
owns, so the leaf needs dispatch and the lazy import is what keeps the
module-level check green. Named in `KNOWN_RUNTIME_CYCLES` with that
reasoning attached, because adding a component to that list without
saying why is a green tick for a regression. The real fix is moving
`_handle_chat_member_update` to a leaf, the way `_stop_button_markup`
moved to `llm_markup` under item 8 -- **item 10's work**, and recorded
there rather than left as a comment.

### Acceptance

* `_selftest` at or under 300 lines -- **111**.
* Every leaf module with its own test -- seven modules, each block
  driven in isolation and checked for shape.
* The whole thing under a mutation harness with a clean-tree preflight --
  `scripts/mutation_harness_selftest.py`, preflight + 14 mutations.

Full suite: 1646 passed, 20 skipped. Golden: six lines across seven slices.