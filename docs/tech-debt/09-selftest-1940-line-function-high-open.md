---
tech_debt: 9
marker: none yet
marker_kind: none          # added when the first leaf module's tripwire lands
state: open
issue: null
---

## 9. `selftest._selftest` is a 1940-line function (HIGH, open)

**Measured 2026-10-22:** `telegram-bot/selftest.py` is 2075 lines;
`_selftest` spans 135–2074, so **1940 of them**. That is 2.4x
`call_llama` (825), which was item 3 and drew months of review attention.

**Why it is not in the registry already.** Item 1 records moving
`_selftest` out of `bot.py` as work that landed. The move happened; the
function did not shrink by a single line. Filing an item as closed on the
strength of a relocation is how this one went missing, and it is the same
mistake item 1 opens by describing in its own text.

**What is at risk.** The class of failure is measured, not hypothetical.
Extracting `call_llama` produced four defects in one pass, none of which
any test caught and all of which compiled and left the suite green:

1. `saw_unavailable_tool` became a local of the new module and existed
   only on the error path — `UnboundLocalError` on every photo call.
2. The first cut swallowed the whole SSE framing block (the `data: `
   prefix, the `[DONE]` break, the `except JSONDecodeError`) because it
   read as boilerplate at the seam. `[DONE]` would have gone into
   `json.loads` on every stream.
3. A tripwire that checked for a substring kept passing after the guard
   it guarded was deleted, because the word also appeared in the comment
   above it.
4. `+= ""` makes `if cc:` and `if cc is not None:` indistinguishable for
   strings; the comment asserted a `content: null` difference that does
   not exist. The real input is a non-string.

**Trigger.** Not scheduled. It is open because it is the largest untested
unit in the repository, not because a feature needs it.

**How to fix.** Cut by block, not by line count — item 3's own text
says the same. Every block moved out gets its own tripwire; a block that
cannot be tested in isolation does not move. Two structural rules from
that extraction apply directly:

- Cut on `ast.end_lineno`, never by searching for the next top-level
  marker. A search-based cut has already swallowed an import block here.
- `test_drift_call_llama_names.py`-style name-drift tripwires must be
  extended to cover the new leaves as they appear, or the drift check
  silently stops covering them.

**Acceptance.** `_selftest` at or under 300 lines, every leaf module with
its own test, and the whole thing under a mutation harness with a
clean-tree preflight.