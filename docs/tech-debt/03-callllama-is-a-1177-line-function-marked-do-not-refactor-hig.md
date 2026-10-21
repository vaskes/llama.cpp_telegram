---
tech_debt: 3
marker: none -- a 1177-line function cannot be pinned by a tripwire; it needs a human watching the bot answer messages
state: open
issue: 2          # filled in when the Issue is created
---

## 3. `call_llama` is a 1177-line function marked DO NOT REFACTOR (HIGH) — **NOT TOUCHED**

**What.** One `async def` holding the tool loop, streaming SSE parsing,
non-streaming fallback, abort handling, backpressure, and fallback text
generation. Cyclomatic complexity in the low hundreds. The source comment says
`DO NOT REFACTOR` — a note left after someone tried and did not finish.

**Why it exists.** It works, and every change to it has historically cost more
than it saved (see the CHANGELOG's hotfix count).

**Cost.** This is where every new feature lands. Each one adds a branch to a
function nobody wants to touch, which is precisely how the complexity got
here.

**Deliberately not started on 2026-10-21**, after items 1, 2 and 4 landed.
This is the one item where a half-refactor is worse than none, and it needs a
person who can watch the bot answer messages while it happens. The other three
were mechanical; this one is not.

**Trigger to fix.** When a feature needs to add a *new kind* of thing to the
turn (a new streaming mode, a new tool-transport, a new abort semantic) rather
than another flag. Splitting it for its own sake produces the abandoned
half-refactor the comment warns about.

**How to fix.** Extract the pieces that have no shared mutable state first:
SSE parsing, the non-streaming fallback, the fallback-text builder. Leave the
tool loop intact until it can be expressed as a state machine over an explicit
`(messages, tools, iteration)` state, not by chopping on line counts.

---
