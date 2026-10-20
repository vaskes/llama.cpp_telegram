from pathlib import Path

p = Path("REVIEW.md")
s = p.read_text()

old = """**Branch:** `main` · **Base:** `8069781` (update.sh fix) · **Head:** `f419abe`
**Scope:** 13 commits, 41 files, +6739 / -201"""

new = """**Branch:** `main` · **Base:** `8069781` (update.sh fix)
**Scope:** 15 commits, 41 files · 445 tests passing

---

## Already found and fixed — do not re-report

Two bugs were found and fixed *before* this review was handed out. They are
listed here so the review does not spend its budget rediscovering them, and so
that a fix landing by accident is still visible.

**1. `reanchor()` moved a variable nobody read.** (`0b6684d`)

It updated the module global `CONTEXT_ANCHOR`, but `get_budget()` returns the
shared `_DEFAULT` instance and `apply()` filters on *that* instance's own
`.anchor`, assigned once in `__init__`. So the anchor advanced, the generation
counter ticked, the log said "reanchored" — and the context window did not
move by a second. `BOT_REANCHOR_MODE=soft` was a silent no-op, which is the
one mode that exists specifically to re-anchor without a restart.

The original tripwire missed it because it asserted on `CONTEXT_ANCHOR` — the
variable the code was *supposed* to care about, rather than the one `apply()`
reads. That is the claim-vs-implementation gap this block of work exists to
prevent, committed inside the module that prevents it.

**2. The daily timer fired an hour late, once a year.** (`4cc6291`)

`seconds_until_next` built its target as `midnight + hour*3600` and added a flat
`86400` when that was already past. A DST day is 23 or 25 hours. Measured in
`America/New_York`: the day before a spring-forward transition it computed
`06:00` instead of `05:00`, then the inner 10-minute re-evaluation found its own
target in the past and pushed it out another 86400 — skipping a full day.

The docstring already claimed "the loop must survive a DST shift". It did not.
Nobody noticed because Moscow has had no DST since 2014; a third of this repo's
200+ forks do.

---

## Still worth looking for

The same class of bug is worth hunting: **something that reports success while
doing nothing, or reports one thing while doing another.** Both of the above
were of that kind — a log line, a counter, or a tripwire that looked healthy
and was not. Ask of every claim below: *is the thing that moves the same thing
that the thing being moved controls?*"""

assert s.count(old) == 1, "header anchor not found"
s = s.replace(old, new, 1)

# Add the found-and-fixed tests to the review scope
old2 = """### 1. The window cannot slide

`CONTEXT_ANCHOR` is a module-level constant. If anything recomputes it per
request, the bot forgets the start of every live conversation, one day at a
time, with no error anywhere.

There are two writers: the module-level assignment and `reanchor()`. Is there
a third path — an import-order side effect, a fork, a reload — that moves it?
`tests/test_t70_context_budget.py` asserts the writer set; does the assertion
actually constrain the AST it thinks it does?"""

new2 = """### 1. The window cannot slide

`CONTEXT_ANCHOR` is a module-level constant, and there are two writers: the
module-level assignment and `reanchor()`. A third path — an import-order side
effect, a fork, a reload — would move it.

> Note: `reanchor()` used to be a no-op for exactly this reason (see above). The
> fix writes `_DEFAULT.anchor` as well. Check that the fix is complete and that
> `describe()` and `/stats` read the same value the filter does. Is there
> anything else in the codebase that reads `CONTEXT_ANCHOR` directly and would
> now disagree with the live instance?"""

assert s.count(old2) == 1, "claim 1 anchor not found"
s = s.replace(old2, new2, 1)

p.write_text(s)
print("REVIEW.md updated")